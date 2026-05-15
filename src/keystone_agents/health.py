"""Offline system health checks for Keystone business agents."""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.model_provider import (
    DEFAULT_PROVIDER,
    KEYSTONE_OPENAI_API_KEY_ENV,
    SUPPORTED_PROVIDERS,
    UnsupportedModelProviderError,
    get_model_config,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.gmail_tool import gmail_oauth_readiness

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_HIGH = "high"
SEVERITY_ERROR = "error"
MASKED_VALUE = "[masked]"
SET_VALUE = "[set]"
MISSING_VALUE = "[missing]"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "prompts"
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
FIXTURES_ROOT = PROJECT_ROOT / "tests" / "fixtures"
MIN_PYTHON_VERSION = (3, 11)
LITELLM_PACKAGE_NAME = "litellm"
OPENAI_PACKAGE_NAME = "openai"

CORE_IMPORTS = (
    "keystone_agents",
    "agents",
    "pydantic",
    "requests",
    "rich",
)

REQUIRED_PROMPTS = (
    "business_research_analyst.md",
    "chief_of_staff.md",
    "gmail_triage.md",
    "keystone_profile.md",
    "opportunity_scout.md",
    "orchestrator.md",
    "outreach_composer.md",
    "safety_policy.md",
)

REQUIRED_TABLES = (
    "agent_runs",
    "emails",
    "companies",
    "opportunities",
    "outreach_drafts",
    "approvals",
    "approval_queue",
    "sources",
    "feedback",
    "tool_events",
    "agent_run_logs",
    "contacts",
    "crm_contexts",
    "follow_up_schedules",
    "outreach_tracking",
    "email_style_profiles",
    "memory_items",
    "memory_index",
    "outreach_examples",
    "work_items",
    "work_item_events",
    "work_item_artifacts",
    "schema_migrations",
)

DRY_RUN_SCRIPTS = (
    "run_keystone_automation.py",
    "run_gmail_triage.py",
    "run_company_research.py",
    "run_opportunity_scout.py",
    "run_outreach_draft.py",
    "run_orchestrator.py",
    "run_chief_of_staff.py",
    "run_keystone_pipeline.py",
)

REQUIRED_FIXTURES = (
    "sample_email_consulting.txt",
    "sample_email_collaboration.txt",
    "sample_email_suspicious.txt",
    "sample_company_curebase.json",
    "sample_company_neuroflow.json",
    "sample_lead_curebase.json",
    "sample_contact_curebase_approved.json",
    "sample_crm_context_curebase.json",
)

ENVIRONMENT_VARIABLES = (
    "AUTO_SEND_EMAIL",
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_DRY_RUN",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_ENABLE_LIVE_CRM",
    "MODEL_PROVIDER",
    "KEYSTONE_OPENAI_API_KEY",
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "LITELLM_BASE_URL",
    "GEMINI_API_KEY",
    "DATABASE_URL",
    "GOOGLE_CREDENTIALS_FILE",
    "GOOGLE_TOKEN_FILE",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
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
    "APIFY_API_TOKEN",
    "BROWSERLESS_API_KEY",
    "KEYSTONE_TRACE_WORKFLOW_NAME",
    "KEYSTONE_TRACE_GROUP_ID",
    "KEYSTONE_TRACE_METADATA",
    "KEYSTONE_TRACING_DISABLED",
    "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
)

VISIBLE_ENVIRONMENT_VALUES = {
    "AUTO_SEND_EMAIL",
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_DRY_RUN",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_ENABLE_LIVE_CRM",
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
    "MODEL_PROVIDER",
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "SEARCH_PROVIDER",
    "SEARXNG_BASE_URL",
    "TAVILY_BASE_URL",
    "TAVILY_SEARCH_DEPTH",
    "TAVILY_MCP_LINK",
    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
    "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
    "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
    "KEYSTONE_TAVILY_USAGE_PATH",
    "KEYSTONE_TRACING_DISABLED",
    "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
}

FALSE_VALUES = {"", "0", "false", "no", "off"}


@dataclass(frozen=True)
class HealthMessage:
    """Human-readable health message."""

    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class CheckItem:
    """Single item within a health check section."""

    name: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    """JSON-serializable health report."""

    generated_at: str
    overall_status: str
    python: dict[str, Any]
    imports: list[CheckItem]
    prompts: list[CheckItem]
    agent_builders: list[CheckItem]
    database: dict[str, Any]
    dry_run_scripts: list[CheckItem]
    environment: list[CheckItem]
    live_integrations: list[CheckItem]
    auto_send_email: dict[str, Any]
    model_provider: dict[str, Any]
    fixtures: list[CheckItem]
    messages: list[HealthMessage]

    def to_dict(self) -> dict[str, Any]:
        """Return a plain JSON-serializable dictionary."""

        return asdict(self)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _status_from_items(items: list[CheckItem]) -> str:
    if any(item.status == STATUS_ERROR for item in items):
        return STATUS_ERROR
    if any(item.status == STATUS_WARNING for item in items):
        return STATUS_WARNING
    return STATUS_OK


def _status_from_messages(messages: list[HealthMessage]) -> str:
    if any(message.severity == SEVERITY_ERROR for message in messages):
        return STATUS_ERROR
    if any(message.severity in {SEVERITY_WARNING, SEVERITY_HIGH} for message in messages):
        return STATUS_WARNING
    return STATUS_OK


def _bool_from_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() not in FALSE_VALUES


def _enabled_env_names(env: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    """Return live/safety flag names that are explicitly truthy."""

    return [name for name in names if _bool_from_env(_env_value(env, name))]


def _dry_run_disabled(env: Mapping[str, str]) -> bool:
    value = _env_value(env, "KEYSTONE_DRY_RUN")
    return value is not None and not _bool_from_env(value)


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _openai_api_key_configured(env: Mapping[str, str]) -> bool:
    return bool(_env_value(env, KEYSTONE_OPENAI_API_KEY_ENV))


def _masked_env_value(name: str, value: str | None) -> str:
    if value is None:
        return MISSING_VALUE
    if name in VISIBLE_ENVIRONMENT_VALUES:
        return value
    return MASKED_VALUE if _looks_sensitive_name(name) else SET_VALUE


def _looks_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        term in lowered
        for term in (
            "api_key",
            "apikey",
            "authorization",
            "credential",
            "metadata",
            "password",
            "secret",
            "token",
        )
    )


def _module_name_for_script(script_name: str) -> str:
    return f"_keystone_health_{script_name.removesuffix('.py')}"


def _load_script_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(_module_name_for_script(path.name), path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _distribution_version(package_name: str) -> str | None:
    try:
        return importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _distribution_requirements(package_name: str) -> list[str]:
    try:
        return list(importlib_metadata.requires(package_name) or [])
    except importlib_metadata.PackageNotFoundError:
        return []


def _requirements_pin_openai_exactly(requirements: list[str]) -> bool:
    for requirement in requirements:
        spec = requirement.split(";", maxsplit=1)[0].replace(" ", "").lower()
        if spec.startswith(f"{OPENAI_PACKAGE_NAME}=="):
            return True
    return False


def check_python_version() -> dict[str, Any]:
    """Return Python runtime details."""

    info = sys.version_info
    supported = (info.major, info.minor) >= MIN_PYTHON_VERSION
    return {
        "status": STATUS_OK if supported else STATUS_ERROR,
        "version": sys.version.split()[0],
        "implementation": sys.implementation.name,
        "major": info.major,
        "minor": info.minor,
        "micro": info.micro,
        "executable": sys.executable,
        "minimum_supported": ".".join(str(part) for part in MIN_PYTHON_VERSION),
    }


def check_imports() -> list[CheckItem]:
    """Check importability for core local package and declared runtime dependencies."""

    items: list[CheckItem] = []
    for module_name in CORE_IMPORTS:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - exercised by environment.
            items.append(
                CheckItem(
                    name=module_name,
                    status=STATUS_ERROR,
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            continue
        items.append(
            CheckItem(
                name=module_name,
                status=STATUS_OK,
                details={"module_file": getattr(module, "__file__", "") or "built-in"},
            )
        )
    return items


def check_prompts(prompts_root: Path = PROMPTS_ROOT) -> list[CheckItem]:
    """Check that required prompt markdown files exist."""

    items: list[CheckItem] = []
    registry_prompt_files = {
        prompt_file for spec in list_agent_specs() for prompt_file in spec.prompt_files
    }
    for filename in sorted(set(REQUIRED_PROMPTS) | registry_prompt_files):
        path = prompts_root / filename
        present = path.is_file()
        size = path.stat().st_size if present else 0
        items.append(
            CheckItem(
                name=filename,
                status=STATUS_OK if present and size > 0 else STATUS_ERROR,
                details={"path": str(path), "present": present, "bytes": size},
            )
        )
    return items


def check_agent_builders() -> list[CheckItem]:
    """Check registered build_*_agent functions construct matching SDK agents."""

    items: list[CheckItem] = []
    for spec in list_agent_specs():
        details: dict[str, Any] = {
            "route_name": spec.route_name,
            "registry_agent_name": spec.agent_name,
            "builder": spec.builder,
            "present": False,
            "callable": False,
        }
        status = STATUS_ERROR
        try:
            builder = spec.resolve_builder()
            expected_output_type = spec.resolve_output_schema()
        except Exception as exc:
            details["error"] = f"{type(exc).__name__}: {exc}"
        else:
            details["present"] = True
            details["callable"] = callable(builder)
            try:
                agent = builder()
            except Exception as exc:
                details["error"] = f"{type(exc).__name__}: {exc}"
            else:
                output_type = getattr(agent, "output_type", None)
                details.update(
                    {
                        "constructed": True,
                        "agent_name": getattr(agent, "name", ""),
                        "has_instructions": bool(getattr(agent, "instructions", "")),
                        "tool_count": len(getattr(agent, "tools", []) or []),
                        "output_type": getattr(output_type, "__name__", str(output_type)),
                        "expected_output_type": expected_output_type.__name__,
                        "output_type_matches_registry": output_type is expected_output_type,
                    }
                )
                status = (
                    STATUS_OK
                    if details["agent_name"] and details["has_instructions"]
                    and details["output_type_matches_registry"]
                    else STATUS_ERROR
                )
        items.append(
            CheckItem(
                name=spec.builder_name,
                status=status,
                details=details,
            )
        )
    return items


def check_database(database_url: str | Path | None = None) -> dict[str, Any]:
    """Check local SQLite accessibility and required tables without live calls."""

    explicit_database_url = database_url is not None or bool(os.getenv("DATABASE_URL"))
    configured_url: str | Path | None = database_url or (
        database_url_from_env() if explicit_database_url else None
    )
    details: dict[str, Any] = {
        "status": STATUS_OK,
        "database_url_source": "explicit" if explicit_database_url else "memory_probe",
        "path": "",
        "required_tables_present": [],
        "required_tables_missing": [],
        "schema_version": None,
    }
    try:
        if configured_url is None or str(configured_url) == ":memory:":
            with tempfile.TemporaryDirectory(prefix="keystone-health-") as tmpdir:
                store = SQLiteStore(Path(tmpdir) / "health.db")
                tables = store.table_names()
                missing = sorted(set(REQUIRED_TABLES) - tables)
                details.update(
                    {
                        "status": STATUS_ERROR if missing else STATUS_OK,
                        "path": ":temporary:",
                        "required_tables_present": sorted(set(REQUIRED_TABLES) & tables),
                        "required_tables_missing": missing,
                        "schema_version": store.current_schema_version(),
                    }
                )
                return details

        store = SQLiteStore(configured_url)
        tables = store.table_names()
        missing = sorted(set(REQUIRED_TABLES) - tables)
        details.update(
            {
                "status": STATUS_ERROR if missing else STATUS_OK,
                "path": store.path,
                "required_tables_present": sorted(set(REQUIRED_TABLES) & tables),
                "required_tables_missing": missing,
                "schema_version": store.current_schema_version(),
            }
        )
    except Exception as exc:
        details.update(
            {
                "status": STATUS_ERROR,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    return details


def check_dry_run_scripts(scripts_root: Path = SCRIPTS_ROOT) -> list[CheckItem]:
    """Check dry-run script entrypoints are importable and expose callable main functions."""

    items: list[CheckItem] = []
    for script_name in DRY_RUN_SCRIPTS:
        path = scripts_root / script_name
        if not path.is_file():
            items.append(
                CheckItem(
                    name=script_name,
                    status=STATUS_ERROR,
                    details={"path": str(path), "present": False},
                )
            )
            continue
        try:
            module = _load_script_module(path)
            has_main = callable(getattr(module, "main", None))
            has_parser = callable(getattr(module, "build_parser", None))
        except Exception as exc:
            items.append(
                CheckItem(
                    name=script_name,
                    status=STATUS_ERROR,
                    details={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
                )
            )
            continue
        items.append(
            CheckItem(
                name=script_name,
                status=STATUS_OK if has_main else STATUS_ERROR,
                details={
                    "path": str(path),
                    "present": True,
                    "main_callable": has_main,
                    "parser_callable": has_parser,
                    "executed": False,
                },
            )
        )
    return items


def check_environment(env: Mapping[str, str] | None = None) -> list[CheckItem]:
    """Return configured environment variables with values masked and safety flags noted."""

    source = os.environ if env is None else env
    items: list[CheckItem] = []
    for name in ENVIRONMENT_VARIABLES:
        value = _env_value(source, name)
        issues: list[str] = []
        if name == "AUTO_SEND_EMAIL" and _bool_from_env(value):
            issues.append("auto-send flag is enabled")
        if name == "KEYSTONE_DRY_RUN" and value is not None and not _bool_from_env(value):
            issues.append("dry-run default is disabled")
        if name == "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA" and _bool_from_env(value):
            issues.append("sensitive trace data is enabled")
        items.append(
            CheckItem(
                name=name,
                status=STATUS_WARNING if issues else STATUS_OK,
                details={
                    "present": value is not None,
                    "value": _masked_env_value(name, value),
                    "masked": value is not None and name not in VISIBLE_ENVIRONMENT_VALUES,
                    "issues": issues,
                },
            )
        )
    return items


def check_live_integrations(env: Mapping[str, str] | None = None) -> list[CheckItem]:
    """Report live integration configuration without validating credentials online."""

    source = os.environ if env is None else env
    live_model_flags = tuple(
        _enabled_env_names(source, ("KEYSTONE_LIVE_MODE",))
        + (["KEYSTONE_DRY_RUN=false"] if _dry_run_disabled(source) else [])
    )
    live_slack_flags = tuple(_enabled_env_names(source, ("KEYSTONE_ENABLE_LIVE_SLACK",)))
    live_search_flags = tuple(_enabled_env_names(source, ("KEYSTONE_ENABLE_LIVE_RESEARCH",)))
    slack_business_flags = tuple(
        _enabled_env_names(
            source,
            (
                "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED",
                "KNI_BUSINESS_AGENTS_BACKGROUND_RUNS",
                "KNI_BUSINESS_AGENTS_LIVE_SDK",
                "KNI_BUSINESS_AGENTS_LIVE_SEARCH",
                "KNI_BUSINESS_AGENTS_LIVE_SLACK",
                "KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS",
                "KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED",
            ),
        )
    )
    slack_token = _env_value(source, "SLACK_BOT_TOKEN")
    slack_channel = _env_value(source, "SLACK_CHANNEL_APPROVALS")
    slack_business_approval_channel = _env_value(
        source, "KNI_BUSINESS_AGENTS_APPROVAL_CHANNEL"
    )
    slack_business_context = _bool_from_env(
        _env_value(source, "KNI_BUSINESS_AGENTS_SLACK_CONTEXT_ENABLED")
    )
    slack_business_live_slack = _bool_from_env(
        _env_value(source, "KNI_BUSINESS_AGENTS_LIVE_SLACK")
    )
    slack_business_background = _bool_from_env(
        _env_value(source, "KNI_BUSINESS_AGENTS_BACKGROUND_RUNS")
    )
    slack_business_history = _bool_from_env(
        _env_value(source, "KNI_BUSINESS_AGENTS_HISTORY_CONTEXT_ENABLED")
    )
    slack_business_required = bool(slack_business_flags)
    slack_business_missing = []
    if (
        slack_business_context
        or slack_business_live_slack
        or slack_business_background
        or slack_business_history
    ) and not slack_token:
        slack_business_missing.append("SLACK_BOT_TOKEN")
    if slack_business_live_slack and not (slack_business_approval_channel or slack_channel):
        slack_business_missing.append(
            "KNI_BUSINESS_AGENTS_APPROVAL_CHANNEL or SLACK_CHANNEL_APPROVALS"
        )
    gmail_details = gmail_oauth_readiness(source)
    gmail_readiness = str(gmail_details["readiness"])
    gmail_status = {
        "disabled": STATUS_OK,
        "ready": STATUS_OK,
        "misconfigured": STATUS_WARNING,
    }.get(gmail_readiness, STATUS_WARNING)
    search_provider = (_env_value(source, "SEARCH_PROVIDER") or "dry-run").lower()
    serper_configured = bool(_env_value(source, "SERPER_API_KEY"))
    searxng_configured = bool(_env_value(source, "SEARXNG_BASE_URL"))
    tavily_configured = bool(_env_value(source, "TAVILY_API_KEY"))

    def status_for(required_now: bool, configured: bool) -> str:
        return STATUS_WARNING if required_now and not configured else STATUS_OK

    return [
        CheckItem(
            name="openai",
            status=status_for(bool(live_model_flags), _openai_api_key_configured(source)),
            details={
                "configured": _openai_api_key_configured(source),
                "required_now": bool(live_model_flags),
                "live_flags": live_model_flags,
                "required_for": "live model execution",
            },
        ),
        CheckItem(
            name="gmail",
            status=gmail_status,
            details={
                **gmail_details,
                "required_now": gmail_readiness != "disabled",
                "live_flags": tuple(_enabled_env_names(source, ("KEYSTONE_ENABLE_LIVE_GMAIL",))),
                "required_for": "live Gmail read, label, and draft creation",
            },
        ),
        CheckItem(
            name="slack",
            status=status_for(bool(live_slack_flags), bool(slack_token and slack_channel)),
            details={
                "configured": bool(slack_token and slack_channel),
                "required_now": bool(live_slack_flags),
                "live_flags": live_slack_flags,
                "bot_token_present": bool(slack_token),
                "approval_channel_present": bool(slack_channel),
                "required_for": "live Slack approval notifications",
            },
        ),
        CheckItem(
            name="slack_business_agents",
            status=(
                STATUS_WARNING
                if slack_business_required and slack_business_missing
                else STATUS_OK
            ),
            details={
                "configured": not slack_business_missing,
                "required_now": slack_business_required,
                "live_flags": slack_business_flags,
                "missing": slack_business_missing,
                "slack_context_enabled": slack_business_context,
                "background_runs_enabled": slack_business_background,
                "live_sdk_enabled": _bool_from_env(
                    _env_value(source, "KNI_BUSINESS_AGENTS_LIVE_SDK")
                ),
                "live_search_enabled": _bool_from_env(
                    _env_value(source, "KNI_BUSINESS_AGENTS_LIVE_SEARCH")
                ),
                "live_slack_enabled": slack_business_live_slack,
                "live_gmail_drafts_enabled": _bool_from_env(
                    _env_value(source, "KNI_BUSINESS_AGENTS_LIVE_GMAIL_DRAFTS")
                ),
                "history_context_enabled": slack_business_history,
                "approval_gated": True,
                "external_send_implemented": False,
                "required_for": "@KNI Slack business-agent bridge",
            },
        ),
        CheckItem(
            name="serper",
            status=status_for(
                bool(live_search_flags) and search_provider == "serper",
                serper_configured,
            ),
            details={
                "configured": serper_configured,
                "required_now": bool(live_search_flags) and search_provider == "serper",
                "live_flags": live_search_flags,
                "search_provider": search_provider,
                "required_for": "live search",
            },
        ),
        CheckItem(
            name="searxng",
            status=status_for(
                bool(live_search_flags) and search_provider == "searxng",
                searxng_configured,
            ),
            details={
                "configured": searxng_configured,
                "required_now": bool(live_search_flags) and search_provider == "searxng",
                "live_flags": live_search_flags,
                "search_provider": search_provider,
                "base_url_present": searxng_configured,
                "api_key_present": bool(_env_value(source, "SEARXNG_API_KEY")),
                "required_for": "live SearXNG search",
            },
        ),
        CheckItem(
            name="tavily",
            status=status_for(
                bool(live_search_flags) and search_provider == "tavily",
                tavily_configured,
            ),
            details={
                "configured": tavily_configured,
                "required_now": bool(live_search_flags) and search_provider == "tavily",
                "live_flags": live_search_flags,
                "search_provider": search_provider,
                "base_url": _env_value(source, "TAVILY_BASE_URL") or "https://api.tavily.com",
                "search_depth": _env_value(source, "TAVILY_SEARCH_DEPTH") or "basic",
                "monthly_credit_limit": _env_value(
                    source,
                    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
                )
                or "1000",
                "monthly_soft_limit": _env_value(source, "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT")
                or "850",
                "credit_enforcement": _env_value(source, "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT")
                or "warn",
                "usage_path_present": bool(_env_value(source, "KEYSTONE_TAVILY_USAGE_PATH")),
                "mcp_link_present": bool(
                    _env_value(source, "TAVILY_MCP_LINK")
                    or _env_value(source, "TAVILY_MCP_link")
                ),
                "required_for": "live Tavily search",
            },
        ),
        CheckItem(
            name="apify",
            status=STATUS_OK,
            details={
                "configured": bool(_env_value(source, "APIFY_API_TOKEN")),
                "required_now": False,
                "required_for": "future live Apify access",
            },
        ),
        CheckItem(
            name="browserless",
            status=STATUS_OK,
            details={
                "configured": bool(_env_value(source, "BROWSERLESS_API_KEY")),
                "required_now": False,
                "required_for": "future live Browserless rendering",
            },
        ),
    ]


def check_auto_send_email(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Report AUTO_SEND_EMAIL status."""

    source = os.environ if env is None else env
    raw = _env_value(source, "AUTO_SEND_EMAIL")
    enabled = _bool_from_env(raw)
    return {
        "status": STATUS_WARNING if enabled else STATUS_OK,
        "present": raw is not None,
        "enabled": enabled,
        "send_capability_enabled": enabled,
        "draft_only_confirmed": not enabled,
        "value": _masked_env_value("AUTO_SEND_EMAIL", raw),
    }


def check_model_provider() -> dict[str, Any]:
    """Report configured model provider without making model calls."""

    try:
        config = get_model_config()
    except UnsupportedModelProviderError as exc:
        return {
            "status": STATUS_ERROR,
            "provider": os.getenv("MODEL_PROVIDER") or DEFAULT_PROVIDER,
            "supported_providers": sorted(SUPPORTED_PROVIDERS),
            "error": str(exc),
        }
    litellm_package_version = _distribution_version(LITELLM_PACKAGE_NAME)
    litellm_requirements = (
        _distribution_requirements(LITELLM_PACKAGE_NAME) if litellm_package_version else []
    )
    litellm_exact_openai_pin = _requirements_pin_openai_exactly(litellm_requirements)
    openai_package_version = _distribution_version(OPENAI_PACKAGE_NAME)
    status = STATUS_OK if config.provider in SUPPORTED_PROVIDERS else STATUS_ERROR
    if status == STATUS_OK and litellm_exact_openai_pin:
        status = STATUS_WARNING
    return {
        "status": status,
        "provider": config.provider,
        "model": config.model,
        "supported_providers": sorted(SUPPORTED_PROVIDERS),
        "api_key_present": config.api_key_present,
        "base_url_present": bool(config.base_url),
        "litellm_base_url_present": bool(config.litellm_base_url),
        "litellm_mode": "gateway",
        "python_litellm_required": False,
        "python_litellm_package_present": bool(litellm_package_version),
        "python_litellm_package_version": litellm_package_version,
        "python_litellm_exact_openai_pin": litellm_exact_openai_pin,
        "openai_package_version": openai_package_version,
    }


def check_fixtures(fixtures_root: Path = FIXTURES_ROOT) -> list[CheckItem]:
    """Check that required deterministic test fixtures are present."""

    items: list[CheckItem] = []
    for filename in REQUIRED_FIXTURES:
        path = fixtures_root / filename
        items.append(
            CheckItem(
                name=filename,
                status=STATUS_OK if path.is_file() else STATUS_ERROR,
                details={"path": str(path), "present": path.is_file()},
            )
        )
    return items


def build_messages(
    *,
    python: dict[str, Any],
    imports: list[CheckItem],
    prompts: list[CheckItem],
    agent_builders: list[CheckItem],
    database: dict[str, Any],
    dry_run_scripts: list[CheckItem],
    environment: list[CheckItem],
    live_integrations: list[CheckItem],
    auto_send_email: dict[str, Any],
    model_provider: dict[str, Any],
    fixtures: list[CheckItem],
    env: Mapping[str, str],
) -> list[HealthMessage]:
    """Build report-level health messages."""

    messages: list[HealthMessage] = []

    if python.get("status") == STATUS_ERROR:
        messages.append(
            HealthMessage(
                code="python_version_unsupported",
                severity=SEVERITY_ERROR,
                message=(
                    f"Python {python.get('version')} is unsupported; "
                    f"minimum is {python.get('minimum_supported')}."
                ),
            )
        )

    for section_name, items in (
        ("imports", imports),
        ("prompts", prompts),
        ("agent_builders", agent_builders),
        ("dry_run_scripts", dry_run_scripts),
        ("fixtures", fixtures),
    ):
        failed = [item.name for item in items if item.status == STATUS_ERROR]
        if failed:
            messages.append(
                HealthMessage(
                    code=f"{section_name}_failed",
                    severity=SEVERITY_ERROR,
                    message=f"{section_name} failed for: {', '.join(failed)}",
                )
            )

    if database.get("status") == STATUS_ERROR:
        missing_tables = database.get("required_tables_missing") or []
        if missing_tables:
            message = f"Database is missing required tables: {', '.join(missing_tables)}."
        else:
            message = f"Database is not accessible: {database.get('error', 'unknown error')}."
        messages.append(
            HealthMessage(code="database_unhealthy", severity=SEVERITY_ERROR, message=message)
        )

    if model_provider.get("status") == STATUS_ERROR:
        messages.append(
            HealthMessage(
                code="model_provider_unhealthy",
                severity=SEVERITY_ERROR,
                message=str(model_provider.get("error") or "Model provider is unsupported."),
            )
        )
    elif model_provider.get("python_litellm_exact_openai_pin"):
        messages.append(
            HealthMessage(
                code="litellm_package_conflict",
                severity=SEVERITY_WARNING,
                message=(
                    "The in-process litellm package pins the OpenAI Python client exactly. "
                    "Keystone supports LiteLLM as an external gateway through "
                    "LITELLM_BASE_URL; keep the litellm proxy in a separate environment."
                ),
            )
        )

    if auto_send_email["enabled"]:
        messages.append(
            HealthMessage(
                code="auto_send_email_enabled",
                severity=SEVERITY_HIGH,
                message="AUTO_SEND_EMAIL is true. Disable it; this repo is draft-only.",
            )
        )

    live_credentials_missing = [
        item.name
        for item in live_integrations
        if item.status == STATUS_WARNING and item.details.get("required_now")
    ]
    if live_credentials_missing:
        messages.append(
            HealthMessage(
                code="live_credentials_missing",
                severity=SEVERITY_WARNING,
                message=(
                    "Live flags are configured but credentials are missing for: "
                    f"{', '.join(live_credentials_missing)}."
                ),
            )
        )

    live_map = {item.name: item for item in live_integrations}
    gmail_configured = bool(live_map["gmail"].details.get("configured"))
    gmail_readiness = str(live_map["gmail"].details.get("readiness") or "")
    slack_token_present = bool(live_map["slack"].details.get("bot_token_present"))
    slack_channel_present = bool(live_map["slack"].details.get("approval_channel_present"))

    if gmail_readiness == "misconfigured":
        gmail_messages = live_map["gmail"].details.get("messages") or []
        messages.append(
            HealthMessage(
                code="gmail_oauth_misconfigured",
                severity=SEVERITY_WARNING,
                message=(
                    "Live Gmail is enabled but OAuth setup is misconfigured: "
                    f"{'; '.join(str(message) for message in gmail_messages)}"
                ),
            )
        )

    if gmail_configured and not slack_channel_present:
        messages.append(
            HealthMessage(
                code="gmail_live_without_approval_queue",
                severity=SEVERITY_WARNING,
                message=(
                    "Gmail live configuration is present but no approval queue channel is set."
                ),
            )
        )
    if slack_token_present and not slack_channel_present:
        messages.append(
            HealthMessage(
                code="slack_live_without_approval_channel",
                severity=SEVERITY_WARNING,
                message="Slack live token is present but SLACK_CHANNEL_APPROVALS is missing.",
            )
        )

    unsafe_environment = [item.name for item in environment if item.status == STATUS_WARNING]
    if unsafe_environment:
        messages.append(
            HealthMessage(
                code="environment_safety_warnings",
                severity=SEVERITY_WARNING,
                message=(
                    f"Environment safety warnings are present for: {', '.join(unsafe_environment)}."
                ),
            )
        )

    return messages


def run_health_check(
    *,
    env: Mapping[str, str] | None = None,
    database_url: str | Path | None = None,
    prompts_root: Path = PROMPTS_ROOT,
    scripts_root: Path = SCRIPTS_ROOT,
    fixtures_root: Path = FIXTURES_ROOT,
) -> HealthReport:
    """Run all health checks without credentials or live network calls."""

    source_env = os.environ if env is None else env
    python = check_python_version()
    imports = check_imports()
    prompts = check_prompts(prompts_root)
    agent_builders = check_agent_builders()
    database = check_database(database_url)
    dry_run_scripts = check_dry_run_scripts(scripts_root)
    environment = check_environment(source_env)
    live_integrations = check_live_integrations(source_env)
    auto_send_email = check_auto_send_email(source_env)
    model_provider = check_model_provider()
    fixtures = check_fixtures(fixtures_root)
    messages = build_messages(
        python=python,
        imports=imports,
        prompts=prompts,
        agent_builders=agent_builders,
        database=database,
        dry_run_scripts=dry_run_scripts,
        environment=environment,
        live_integrations=live_integrations,
        auto_send_email=auto_send_email,
        model_provider=model_provider,
        fixtures=fixtures,
        env=source_env,
    )
    section_statuses = [
        str(python["status"]),
        _status_from_items(imports),
        _status_from_items(prompts),
        _status_from_items(agent_builders),
        str(database["status"]),
        _status_from_items(dry_run_scripts),
        _status_from_items(live_integrations),
        str(auto_send_email["status"]),
        str(model_provider["status"]),
        _status_from_items(fixtures),
        _status_from_messages(messages),
    ]
    if STATUS_ERROR in section_statuses:
        overall_status = STATUS_ERROR
    elif STATUS_WARNING in section_statuses:
        overall_status = STATUS_WARNING
    else:
        overall_status = STATUS_OK

    return HealthReport(
        generated_at=_utc_now(),
        overall_status=overall_status,
        python=python,
        imports=imports,
        prompts=prompts,
        agent_builders=agent_builders,
        database=database,
        dry_run_scripts=dry_run_scripts,
        environment=environment,
        live_integrations=live_integrations,
        auto_send_email=auto_send_email,
        model_provider=model_provider,
        fixtures=fixtures,
        messages=messages,
    )


def report_to_json(report: HealthReport) -> str:
    """Serialize a health report as stable JSON."""

    return json.dumps(report.to_dict(), ensure_ascii=True, indent=2, sort_keys=True)


def format_health_report(report: HealthReport, *, verbose: bool = False) -> str:
    """Render a concise plain-text health report."""

    lines = [
        "Keystone health check",
        f"Overall status: {report.overall_status}",
        f"Python: {report.python['version']} ({report.python['implementation']})",
        (
            "Model provider: "
            f"{report.model_provider.get('provider')} / {report.model_provider.get('model')}"
        ),
        (
            "Database: "
            f"{report.database.get('status')} "
            f"({report.database.get('database_url_source')})"
        ),
        (
            "Auto-send: "
            f"{'enabled' if report.auto_send_email.get('enabled') else 'disabled'} "
            f"(draft-only confirmed={report.auto_send_email.get('draft_only_confirmed')})"
        ),
    ]

    if report.messages:
        lines.extend(["", "Messages:"])
        for message in report.messages:
            lines.append(f"- [{message.severity}] {message.code}: {message.message}")

    if verbose:
        lines.extend(["", "Live integrations:"])
        for item in report.live_integrations:
            configured = item.details.get("configured")
            readiness = item.details.get("readiness")
            suffix = f" readiness={readiness}" if readiness else ""
            lines.append(f"- {item.name}: {item.status} configured={configured}{suffix}")

        lines.extend(["", "Environment:"])
        for item in report.environment:
            value = item.details.get("value")
            lines.append(f"- {item.name}: {item.status} value={value}")

        lines.extend(["", "Dry-run scripts:"])
        for item in report.dry_run_scripts:
            lines.append(f"- {item.name}: {item.status}")

        lines.extend(["", "Fixtures:"])
        for item in report.fixtures:
            lines.append(f"- {item.name}: {item.status}")

    return "\n".join(lines)
