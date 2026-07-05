"""Side-effect-free environment contract for Keystone child runtimes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keystone_agents.model_provider import RUNTIME_AGENT_MODEL_SPECS

KEYSTONE_ENV_CONTRACT_SCHEMA = "keystone.business_agent_environment_contract.v1"
KEYSTONE_ENV_CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class EnvVarContract:
    name: str
    category: str
    secret: bool = False
    display_safety: str = "safe"
    default_notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "secret": self.secret,
            "display_safety": self.display_safety,
            "default_notes": self.default_notes,
        }


_BASE_ENV_VARS: tuple[EnvVarContract, ...] = (
    EnvVarContract(
        "AUTO_SEND_EMAIL",
        "safety",
        default_notes="Must remain false/disabled for Slack-owned child runs.",
    ),
    EnvVarContract(
        "DATABASE_URL",
        "storage",
        display_safety="path_or_url",
        default_notes="Defaults to local SQLite unless overridden.",
    ),
    EnvVarContract(
        "KEYSTONE_CONTEXT_CONFIG_OVERRIDE",
        "context",
        default_notes="Defaults to false; true lets linked context repo env override local read config.",
    ),
    EnvVarContract(
        "KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS",
        "context",
        default_notes="Comma-separated allowlist of linked context repo env keys that may override local values.",
    ),
    EnvVarContract(
        "KEYSTONE_CONTEXT_CONFIG_REPO",
        "context",
        display_safety="path",
        default_notes="Optional path to a sibling repo .env for allowlisted read context config.",
    ),
    EnvVarContract(
        "KNI_BUSINESS_AGENTS_LANGGRAPH",
        "orchestration",
        default_notes=(
            "Slack parent flag is scrubbed for child runs so KBA backend policy "
            "or explicit KEYSTONE_WORKITEM_LANGGRAPH test overrides own graph selection."
        ),
    ),
    EnvVarContract(
        "FIRECRAWL_API_KEY",
        "website_extraction",
        secret=True,
        display_safety="secret",
    ),
    EnvVarContract(
        "FIRECRAWL_BASE_URL",
        "website_extraction",
        default_notes="Defaults to https://api.firecrawl.dev.",
    ),
    EnvVarContract("EXA_API_KEY", "search", secret=True, display_safety="secret"),
    EnvVarContract("EXA_API_KEY_ID", "search", display_safety="identifier"),
    EnvVarContract("EXA_API_KEY_NAME", "search", display_safety="identifier"),
    EnvVarContract(
        "EXA_BASE_URL",
        "search",
        display_safety="url",
        default_notes="Defaults to https://api.exa.ai.",
    ),
    EnvVarContract("EXA_SERVICE_API_KEY", "search", secret=True, display_safety="secret"),
    EnvVarContract("GEMINI_API_KEY", "model", secret=True, display_safety="secret"),
    EnvVarContract("GMAIL_CLIENT_ID", "gmail", display_safety="identifier"),
    EnvVarContract("GMAIL_CLIENT_SECRET", "gmail", secret=True, display_safety="secret"),
    EnvVarContract("GOOGLE_CREDENTIALS_FILE", "gmail", display_safety="path"),
    EnvVarContract("GOOGLE_TOKEN_FILE", "gmail", secret=True, display_safety="secret_path"),
    EnvVarContract(
        "KEYSTONE_AGENT_HTML_REVIEW",
        "website_extraction",
        default_notes="Defaults to disabled unless explicitly enabled.",
    ),
    EnvVarContract("KEYSTONE_AGENT_HTML_REVIEW_MAX_CHARS", "website_extraction"),
    EnvVarContract("KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES", "website_extraction"),
    EnvVarContract("KEYSTONE_AGENT_HTML_REVIEW_MIN_CLAIMS", "website_extraction"),
    EnvVarContract(
        "KEYSTONE_AGENT_RUN_BUDGET_USD",
        "model",
        default_notes="Defaults to the repo budget policy.",
    ),
    EnvVarContract(
        "KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK",
        "search",
        default_notes="Defaults to enabled by retrieval policy.",
    ),
    EnvVarContract("KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN", "search"),
    EnvVarContract("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "search"),
    EnvVarContract(
        "KEYSTONE_DEFAULT_MODEL",
        "model",
        default_notes="Fallback model when agent-specific settings are absent.",
    ),
    EnvVarContract(
        "KEYSTONE_DRY_RUN",
        "safety",
        default_notes="Defaults to true for local CLI paths.",
    ),
    EnvVarContract(
        "KEYSTONE_ENABLE_LIVE_GMAIL",
        "gmail",
        default_notes="Requires explicit live mode and credentials.",
    ),
    EnvVarContract(
        "KEYSTONE_ENABLE_LIVE_RESEARCH",
        "search",
        default_notes="Requires live mode; false in dry-run paths.",
    ),
    EnvVarContract(
        "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
        "website_extraction",
        default_notes="Defaults to disabled unless explicitly enabled.",
    ),
    EnvVarContract(
        "KEYSTONE_EXA_SEARCH_FALLBACK",
        "search",
        default_notes=(
            "Defaults to true when Exa is configured. Intended for capped semantic "
            "deepening under the free-tier credit budget."
        ),
    ),
    EnvVarContract(
        "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN",
        "search",
        default_notes="Defaults to 2 for opt-in Exa fallback/deepening runs.",
    ),
    EnvVarContract(
        "KEYSTONE_EXA_MONTHLY_FREE_REQUEST_LIMIT",
        "search",
        default_notes="Defaults to 1000 credits/month for the Exa free tier.",
    ),
    EnvVarContract("KEYSTONE_HOME", "storage", display_safety="path"),
    EnvVarContract("KEYSTONE_LIVE_MODE", "safety", default_notes="Defaults to false."),
    EnvVarContract("KEYSTONE_LIVE_MODEL_MAX_RETRIES", "model"),
    EnvVarContract("KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS", "model"),
    EnvVarContract("KEYSTONE_OPENAI_API_KEY", "model", secret=True, display_safety="secret"),
    EnvVarContract("KEYSTONE_OPENAI_BASE_URL", "model", display_safety="url"),
    EnvVarContract("KEYSTONE_OPENAI_FALLBACK_BASE_URL", "model", display_safety="url"),
    EnvVarContract("KEYSTONE_OPENAI_FALLBACK_MODEL", "model"),
    EnvVarContract(
        "KEYSTONE_OPENAI_MODEL",
        "model",
        default_notes="Defaults to the repo model provider policy.",
    ),
    EnvVarContract("KEYSTONE_RUNTIME_STATE_DIR", "storage", display_safety="path"),
    EnvVarContract(
        "KEYSTONE_SEARXNG_TRANSIENT",
        "search",
        default_notes="Slack child runs default to true.",
    ),
    EnvVarContract(
        "KEYSTONE_SERPER_ENABLED",
        "search",
        default_notes=(
            "Disabled while Serper API credits are unavailable; set true only after "
            "credits are restored."
        ),
    ),
    EnvVarContract(
        "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
        "search",
        default_notes="Defaults to warn.",
    ),
    EnvVarContract(
        "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
        "search",
        default_notes="Defaults to 1000.",
    ),
    EnvVarContract(
        "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
        "search",
        default_notes="Defaults to 850.",
    ),
    EnvVarContract(
        "KEYSTONE_TAVILY_SEARCH_FALLBACK",
        "search",
        default_notes="Slack child runs default to false.",
    ),
    EnvVarContract(
        "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN",
        "search",
        default_notes="Defaults to 2 for opt-in Tavily deepening runs.",
    ),
    EnvVarContract("KEYSTONE_TAVILY_USAGE_PATH", "search", display_safety="path"),
    EnvVarContract("KEYSTONE_TRACE_GROUP_ID", "tracing", display_safety="identifier"),
    EnvVarContract(
        "KEYSTONE_TRACE_METADATA",
        "tracing",
        display_safety="metadata",
        default_notes="Must not include secrets or sensitive content.",
    ),
    EnvVarContract("KEYSTONE_TRACE_WORKFLOW_NAME", "tracing"),
    EnvVarContract(
        "KEYSTONE_WEBSITE_EXTRACTOR",
        "website_extraction",
        default_notes="Defaults to trafilatura.",
    ),
    EnvVarContract("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "website_extraction"),
    EnvVarContract("LITELLM_BASE_URL", "model", display_safety="url"),
    EnvVarContract("MODEL_PROVIDER", "model", default_notes="Defaults to openai."),
    EnvVarContract(
        "OPENAI_API_KEY",
        "model",
        secret=True,
        display_safety="secret",
        default_notes="Compatibility fallback; KEYSTONE_OPENAI_API_KEY is preferred.",
    ),
    EnvVarContract(
        "SEARCH_PROVIDER",
        "search",
        default_notes="Defaults to dry-run unless live retrieval is explicitly enabled.",
    ),
    EnvVarContract("SEARXNG_API_KEY", "search", secret=True, display_safety="secret"),
    EnvVarContract(
        "SEARXNG_BASE_URL",
        "search",
        display_safety="url",
        default_notes="Slack child runs default to http://127.0.0.1:18080.",
    ),
    EnvVarContract("SERPER_API_KEY", "search", secret=True, display_safety="secret"),
    EnvVarContract("SLACK_BOT_TOKEN", "slack", secret=True, display_safety="secret"),
    EnvVarContract("SLACK_CHANNEL_APPROVALS", "slack", display_safety="channel"),
    EnvVarContract("TAVILY_API_KEY", "search", secret=True, display_safety="secret"),
    EnvVarContract(
        "TAVILY_BASE_URL",
        "search",
        display_safety="url",
        default_notes="Defaults to https://api.tavily.com.",
    ),
    EnvVarContract("TAVILY_MCP_LINK", "search", display_safety="url"),
    EnvVarContract(
        "TAVILY_MCP_link",
        "search",
        display_safety="url",
        default_notes="Legacy mixed-case compatibility key.",
    ),
    EnvVarContract("TAVILY_SEARCH_DEPTH", "search", default_notes="Defaults to basic."),
)


def _runtime_model_env_vars() -> tuple[EnvVarContract, ...]:
    contracts: list[EnvVarContract] = []
    for spec in RUNTIME_AGENT_MODEL_SPECS.values():
        contracts.extend(
            [
                EnvVarContract(
                    spec.model_env,
                    "model",
                    default_notes=f"Model override for {spec.agent_name}.",
                ),
                EnvVarContract(
                    spec.provider_env,
                    "model",
                    default_notes=f"Provider override for {spec.agent_name}.",
                ),
                EnvVarContract(
                    spec.base_url_env,
                    "model",
                    display_safety="url",
                    default_notes=f"Base URL override for {spec.agent_name}.",
                ),
            ]
        )
    return tuple(contracts)


def keystone_env_var_contracts() -> tuple[EnvVarContract, ...]:
    """Return KBA-owned env vars that may influence child process behavior."""

    by_name = {item.name: item for item in (*_BASE_ENV_VARS, *_runtime_model_env_vars())}
    return tuple(by_name[name] for name in sorted(by_name))


def keystone_env_contract() -> dict[str, Any]:
    """Return the side-effect-free KBA environment contract metadata."""

    return {
        "schema": KEYSTONE_ENV_CONTRACT_SCHEMA,
        "version": KEYSTONE_ENV_CONTRACT_VERSION,
        "env_vars": [item.as_dict() for item in keystone_env_var_contracts()],
        "slack_child_env": {
            "scrub_parent_env_vars": [item.name for item in keystone_env_var_contracts()],
            "overlay_order": [
                "Slack process env and Slack .env are read first.",
                "KBA-owned env vars from that parent env are removed.",
                "The KBA repo .env is overlaid.",
                "Bridge-supplied per-command env overrides are applied last by Keystone Slack.",
            ],
            "default_overlays": {
                "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
                "KEYSTONE_SEARXNG_TRANSIENT": "true",
                "KEYSTONE_TAVILY_SEARCH_FALLBACK": "false",
                "KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN": "2",
                "KEYSTONE_EXA_SEARCH_FALLBACK": "true",
                "KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN": "2",
            },
        },
        "notes": [
            "This contract is metadata only and does not read environment values.",
            (
                "Secret/display safety labels describe logging and UI handling; "
                "they do not authorize live side effects."
            ),
            (
                "Live integrations still require explicit runtime flags, credentials, "
                "and downstream approval gates."
            ),
        ],
    }


def allowed_env_var_names() -> frozenset[str]:
    """Return KBA env var names listed by the contract."""

    return frozenset(item.name for item in keystone_env_var_contracts())
