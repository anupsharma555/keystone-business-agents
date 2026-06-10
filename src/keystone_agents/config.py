"""Configuration helpers for safe local runs."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from keystone_agents.costing import configured_agent_run_budget_usd
from keystone_agents.model_provider import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_WORKFLOW_NAME,
    KEYSTONE_OPENAI_BASE_URL_ENV,
    KEYSTONE_OPENAI_MODEL_ENV,
    RUNTIME_AGENT_MODEL_SPECS,
    get_runtime_agent_model_config,
    get_trace_config,
    openai_api_key_from_env,
)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared, fallback keeps tests portable.
    load_dotenv = None


FALSE_VALUES = {"", "0", "false", "no", "off"}
LIVE_SPECIALIST_TEST_DEFAULT = False
DEFAULT_LOCAL_DATABASE_NAME = "keystone_agents.db"


@dataclass(frozen=True)
class LiveCredentialRequirement:
    """Environment-backed credential required before a live CLI side effect."""

    setting_name: str
    env_name: str


SLACK_APPROVAL_CREDENTIALS = (
    LiveCredentialRequirement("slack_bot_token", "SLACK_BOT_TOKEN"),
    LiveCredentialRequirement("slack_channel_approvals", "SLACK_CHANNEL_APPROVALS"),
)


@dataclass(frozen=True)
class Settings:
    """Runtime settings.

    `live_mode` defaults to false so local scripts and tests cannot accidentally call external APIs.
    """

    live_mode: bool = False
    model_provider: str = DEFAULT_PROVIDER
    default_model: str = DEFAULT_MODEL
    openai_model: str = DEFAULT_MODEL
    runtime_agent_models: dict[str, dict[str, str | bool | None]] = field(default_factory=dict)
    agent_run_budget_usd: float = 0.25
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    gemini_api_key: str | None = None
    litellm_base_url: str | None = None
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    google_credentials_file: str | None = None
    google_token_file: str | None = None
    slack_bot_token: str | None = None
    slack_channel_approvals: str | None = None
    search_provider: str = "dry-run"
    exa_api_key: str | None = None
    exa_base_url: str = "https://api.exa.ai"
    exa_service_api_key: str | None = None
    exa_api_key_id: str | None = None
    exa_api_key_name: str | None = None
    exa_monthly_free_request_limit: int = 1000
    serper_enabled: bool = False
    serper_api_key: str | None = None
    searxng_base_url: str | None = None
    searxng_api_key: str | None = None
    tavily_api_key: str | None = None
    tavily_base_url: str = "https://api.tavily.com"
    tavily_search_depth: str = "basic"
    tavily_mcp_link: str | None = None
    tavily_monthly_credit_limit: int = 1000
    tavily_monthly_soft_limit: int = 850
    tavily_credit_enforcement: str = "warn"
    tavily_usage_path: str | None = None
    apify_api_token: str | None = None
    browserless_api_key: str | None = None
    website_extractor: str = "trafilatura"
    firecrawl_api_key: str | None = None
    firecrawl_base_url: str = "https://api.firecrawl.dev"
    workflow_name: str = DEFAULT_WORKFLOW_NAME
    group_id: str | None = None
    trace_metadata: dict[str, str | int | float | bool | None] | None = None
    tracing_disabled: bool = False
    trace_include_sensitive_data: bool = False


def parse_bool(value: str | None) -> bool:
    """Parse environment-style booleans."""

    if value is None:
        return False
    return value.strip().lower() not in FALSE_VALUES


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_int(name: str, default: int) -> int:
    value = _env_value(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _mapping_env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def keystone_home(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the optional Keystone local runtime home."""

    value = _env_value("KEYSTONE_HOME") if env is None else _mapping_env_value(env, "KEYSTONE_HOME")
    return Path(value).expanduser() if value else None


def runtime_state_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """Return the optional local runtime state directory."""

    explicit = (
        _env_value("KEYSTONE_RUNTIME_STATE_DIR")
        if env is None
        else _mapping_env_value(env, "KEYSTONE_RUNTIME_STATE_DIR")
    )
    if explicit:
        return Path(explicit).expanduser()
    home = keystone_home(env)
    return home / "state" if home else None


def default_database_url(env: Mapping[str, str] | None = None) -> str:
    """Return the default SQLite URL after environment fallback policy."""

    explicit = (
        _env_value("DATABASE_URL") if env is None else _mapping_env_value(env, "DATABASE_URL")
    )
    if explicit:
        return explicit
    state_dir = runtime_state_dir(env)
    if state_dir is not None:
        return f"sqlite:///{state_dir / DEFAULT_LOCAL_DATABASE_NAME}"
    return f"sqlite:///{DEFAULT_LOCAL_DATABASE_NAME}"


def cli_default_dry_run(env: Mapping[str, str] | None = None) -> bool:
    """Return the default CLI dry-run posture from environment policy."""

    if env is None:
        value = _env_value("KEYSTONE_DRY_RUN")
    else:
        value = _mapping_env_value(env, "KEYSTONE_DRY_RUN")
    if value is None:
        return not LIVE_SPECIALIST_TEST_DEFAULT
    return parse_bool(value)


def cli_live_test_stage_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the operator explicitly enabled the live specialist test stage."""

    if env is None:
        raw_live_mode = _env_value("KEYSTONE_LIVE_MODE")
    else:
        raw_live_mode = _mapping_env_value(env, "KEYSTONE_LIVE_MODE")
    if raw_live_mode is None:
        return LIVE_SPECIALIST_TEST_DEFAULT and not cli_default_dry_run(env)
    live_mode = parse_bool(raw_live_mode)
    return live_mode and not cli_default_dry_run(env)


def cli_default_live_research(env: Mapping[str, str] | None = None) -> bool:
    """Return whether research/search CLIs should default to live retrieval."""

    if env is None:
        raw_enabled = _env_value("KEYSTONE_ENABLE_LIVE_RESEARCH")
    else:
        raw_enabled = _mapping_env_value(env, "KEYSTONE_ENABLE_LIVE_RESEARCH")
    if raw_enabled is None:
        return cli_live_test_stage_enabled(env)
    enabled = parse_bool(raw_enabled)
    return cli_live_test_stage_enabled(env) and enabled


def cli_default_live_gmail(env: Mapping[str, str] | None = None) -> bool:
    """Return whether Gmail CLIs should default to live Gmail access."""

    if env is None:
        raw_enabled = _env_value("KEYSTONE_ENABLE_LIVE_GMAIL")
    else:
        raw_enabled = _mapping_env_value(env, "KEYSTONE_ENABLE_LIVE_GMAIL")
    if raw_enabled is None:
        return cli_live_test_stage_enabled(env)
    enabled = parse_bool(raw_enabled)
    return cli_live_test_stage_enabled(env) and enabled


def cli_default_live_sdk(env: Mapping[str, str] | None = None) -> bool:
    """Return whether SDK-only specialist live runs should default to live execution."""

    return cli_live_test_stage_enabled(env)


def _model_env_value(pre_dotenv_env: Mapping[str, str]) -> str:
    explicit_model = _mapping_env_value(
        pre_dotenv_env, KEYSTONE_OPENAI_MODEL_ENV
    ) or _mapping_env_value(pre_dotenv_env, "KEYSTONE_DEFAULT_MODEL")
    return (
        explicit_model
        or _env_value(KEYSTONE_OPENAI_MODEL_ENV)
        or _env_value("KEYSTONE_DEFAULT_MODEL")
        or DEFAULT_MODEL
    )


def load_settings(env_file: str | Path | None = None, *, force_dotenv: bool = False) -> Settings:
    """Load settings from environment variables and an optional `.env` file."""

    pre_dotenv_env = dict(os.environ)
    if load_dotenv is not None and (
        force_dotenv or not parse_bool(os.getenv("PYTHON_DOTENV_DISABLED"))
    ):
        load_dotenv(dotenv_path=env_file)

    model = _model_env_value(pre_dotenv_env)
    trace_config = get_trace_config()
    runtime_agent_models = {
        agent_name: get_runtime_agent_model_config(agent_name).as_log_dict()
        for agent_name in RUNTIME_AGENT_MODEL_SPECS
    }
    return Settings(
        live_mode=parse_bool(os.getenv("KEYSTONE_LIVE_MODE")),
        model_provider=_env_value("MODEL_PROVIDER") or DEFAULT_PROVIDER,
        default_model=model,
        openai_model=model,
        runtime_agent_models=runtime_agent_models,
        agent_run_budget_usd=float(configured_agent_run_budget_usd()),
        openai_api_key=openai_api_key_from_env(),
        openai_base_url=_env_value(KEYSTONE_OPENAI_BASE_URL_ENV),
        gemini_api_key=_env_value("GEMINI_API_KEY"),
        litellm_base_url=_env_value("LITELLM_BASE_URL"),
        gmail_client_id=_env_value("GMAIL_CLIENT_ID"),
        gmail_client_secret=_env_value("GMAIL_CLIENT_SECRET"),
        google_credentials_file=_env_value("GOOGLE_CREDENTIALS_FILE"),
        google_token_file=_env_value("GOOGLE_TOKEN_FILE"),
        slack_bot_token=_env_value("SLACK_BOT_TOKEN"),
        slack_channel_approvals=_env_value("SLACK_CHANNEL_APPROVALS"),
        search_provider=(
            _mapping_env_value(pre_dotenv_env, "SEARCH_PROVIDER")
            or _env_value("SEARCH_PROVIDER")
            or "dry-run"
        ).lower(),
        exa_api_key=_env_value("EXA_API_KEY"),
        exa_base_url=_env_value("EXA_BASE_URL") or "https://api.exa.ai",
        exa_service_api_key=_env_value("EXA_SERVICE_API_KEY"),
        exa_api_key_id=_env_value("EXA_API_KEY_ID"),
        exa_api_key_name=_env_value("EXA_API_KEY_NAME"),
        exa_monthly_free_request_limit=_env_int("KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT", 1000),
        serper_enabled=parse_bool(os.getenv("KEYSTONE_SERPER_ENABLED")),
        serper_api_key=_env_value("SERPER_API_KEY"),
        searxng_base_url=_env_value("SEARXNG_BASE_URL"),
        searxng_api_key=_env_value("SEARXNG_API_KEY"),
        tavily_api_key=_env_value("TAVILY_API_KEY"),
        tavily_base_url=_env_value("TAVILY_BASE_URL") or "https://api.tavily.com",
        tavily_search_depth=(_env_value("TAVILY_SEARCH_DEPTH") or "basic").lower(),
        tavily_mcp_link=_env_value("TAVILY_MCP_LINK") or _env_value("TAVILY_MCP_link"),
        tavily_monthly_credit_limit=_env_int("KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT", 1000),
        tavily_monthly_soft_limit=_env_int("KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT", 850),
        tavily_credit_enforcement=(
            _env_value("KEYSTONE_TAVILY_CREDIT_ENFORCEMENT") or "warn"
        ).lower(),
        tavily_usage_path=_env_value("KEYSTONE_TAVILY_USAGE_PATH"),
        apify_api_token=_env_value("APIFY_API_TOKEN"),
        browserless_api_key=_env_value("BROWSERLESS_API_KEY"),
        website_extractor=(_env_value("KEYSTONE_WEBSITE_EXTRACTOR") or "trafilatura").lower(),
        firecrawl_api_key=_env_value("FIRECRAWL_API_KEY"),
        firecrawl_base_url=_env_value("FIRECRAWL_BASE_URL") or "https://api.firecrawl.dev",
        workflow_name=trace_config.workflow_name,
        group_id=trace_config.group_id,
        trace_metadata=dict(trace_config.trace_metadata) or None,
        tracing_disabled=trace_config.tracing_disabled,
        trace_include_sensitive_data=trace_config.trace_include_sensitive_data,
    )


def with_cli_environment(
    env_file: str | Path = ".env",
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    """Load a repo-local dotenv file for a CLI entrypoint, then restore the prior env."""

    def decorator(func: Callable[..., object]) -> Callable[..., object]:
        @wraps(func)
        def wrapped(*args: object, **kwargs: object) -> object:
            snapshot = dict(os.environ)
            load_settings(env_file=env_file)
            try:
                return func(*args, **kwargs)
            finally:
                os.environ.clear()
                os.environ.update(snapshot)

        return wrapped

    return decorator


def require_live_mode(settings: Settings) -> None:
    """Raise if a caller attempts a live-only path without explicit live mode."""

    if not settings.live_mode:
        raise RuntimeError("Live mode is disabled. Set KEYSTONE_LIVE_MODE=true explicitly.")


def require_cli_live_confirmation(
    *,
    dry_run: bool,
    live_flag: bool,
    flag_name: str,
    live_action: str,
    required_credentials: Sequence[LiveCredentialRequirement] = (),
    settings: Settings | None = None,
) -> Settings | None:
    """Validate that a CLI live path is explicit and credentialed.

    This helper does not enable live mode by itself. It only confirms that a
    caller requested a named live flag, disabled dry-run, and supplied any
    credentials the CLI can validate before invoking external tools.
    """

    if live_flag and dry_run:
        raise RuntimeError(f"{flag_name} requires --no-dry-run before {live_action}.")
    if not live_flag and not dry_run:
        raise RuntimeError(
            f"{live_action} requires {flag_name}. Re-run with --dry-run or pass "
            f"{flag_name} with --no-dry-run."
        )
    if not live_flag:
        return settings

    resolved_settings = settings
    if required_credentials:
        resolved_settings = resolved_settings or load_settings()
        missing = [
            requirement.env_name
            for requirement in required_credentials
            if not getattr(resolved_settings, requirement.setting_name, None)
        ]
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(
                f"{names} required for {flag_name}. Set them in the environment or .env, "
                f"or run without {flag_name}."
            )
    return resolved_settings
