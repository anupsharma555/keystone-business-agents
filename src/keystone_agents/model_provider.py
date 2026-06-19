"""Minimal model provider configuration for OpenAI-compatible execution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_BUSINESS_AGENT_DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_ORCHESTRATOR_DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_FALLBACK_DEFAULT_MODEL = OPENAI_BUSINESS_AGENT_DEFAULT_MODEL
GEMINI_FLASH_DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_GMAIL_TRIAGE_DEFAULT_MODEL = GEMINI_FLASH_DEFAULT_MODEL
GEMINI_OUTREACH_COMPOSER_DEFAULT_MODEL = GEMINI_FLASH_DEFAULT_MODEL
DEFAULT_WORKFLOW_NAME = "Keystone live SDK run"
KEYSTONE_OPENAI_API_KEY_ENV = "KEYSTONE_OPENAI_API_KEY"
KEYSTONE_OPENAI_MODEL_ENV = "KEYSTONE_OPENAI_MODEL"
KEYSTONE_OPENAI_BASE_URL_ENV = "KEYSTONE_OPENAI_BASE_URL"
KEYSTONE_OPENAI_FALLBACK_MODEL_ENV = "KEYSTONE_OPENAI_FALLBACK_MODEL"
KEYSTONE_OPENAI_FALLBACK_BASE_URL_ENV = "KEYSTONE_OPENAI_FALLBACK_BASE_URL"
KEYSTONE_ENABLE_GEMINI_FALLBACK_ENV = "KEYSTONE_ENABLE_GEMINI_FALLBACK"
KEYSTONE_GEMINI_FALLBACK_MODEL_ENV = "KEYSTONE_GEMINI_FALLBACK_MODEL"
KEYSTONE_GEMINI_FALLBACK_BASE_URL_ENV = "KEYSTONE_GEMINI_FALLBACK_BASE_URL"
GEMINI_PROVIDER = "gemini"
GEMINI_OPENAI_COMPAT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
SUPPORTED_PROVIDERS = frozenset({DEFAULT_PROVIDER, GEMINI_PROVIDER})
OPENAI_COMPATIBLE_GATEWAY_PROVIDERS = frozenset({GEMINI_PROVIDER})
MASKED_SECRET = "[masked]"
FALSE_VALUES = {"", "0", "false", "no", "off"}
TRUE_VALUES = {"1", "true", "yes", "on"}

TRACE_METADATA_MAX_STRING_LENGTH = 200
FORBIDDEN_TRACE_METADATA_KEY_TERMS = (
    "api_key",
    "apikey",
    "authorization",
    "body",
    "content",
    "cookie",
    "credential",
    "diagnosis",
    "draft",
    "message",
    "password",
    "patient",
    "phi",
    "prompt",
    "secret",
    "token",
    "treatment",
)
SENSITIVE_TRACE_METADATA_VALUE_TERMS = (
    "diagnosis",
    "dob",
    "hipaa",
    "medical record",
    "mrn",
    "patient",
    "ssn",
    "treatment",
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{10,}\b", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
)

TraceMetadataValue = str | int | float | bool | None
TraceMetadata = Mapping[str, TraceMetadataValue]


@dataclass(frozen=True)
class RuntimeAgentModelSpec:
    """Environment contract for one Keystone runtime agent model profile."""

    agent_name: str
    model_env: str
    provider_env: str
    base_url_env: str
    default_model: str | None = None
    default_provider: str | None = None


RUNTIME_AGENT_MODEL_SPECS: dict[str, RuntimeAgentModelSpec] = {
    "orchestrator": RuntimeAgentModelSpec(
        agent_name="orchestrator",
        model_env="KEYSTONE_ORCHESTRATOR_MODEL",
        provider_env="KEYSTONE_ORCHESTRATOR_MODEL_PROVIDER",
        base_url_env="KEYSTONE_ORCHESTRATOR_BASE_URL",
        default_model=OPENAI_ORCHESTRATOR_DEFAULT_MODEL,
    ),
    "gmail_triage": RuntimeAgentModelSpec(
        agent_name="gmail_triage",
        model_env="KEYSTONE_GMAIL_TRIAGE_MODEL",
        provider_env="KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER",
        base_url_env="KEYSTONE_GMAIL_TRIAGE_BASE_URL",
        default_model=OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
    ),
    "business_research_analyst": RuntimeAgentModelSpec(
        agent_name="business_research_analyst",
        model_env="KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
        provider_env="KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL_PROVIDER",
        base_url_env="KEYSTONE_BUSINESS_RESEARCH_ANALYST_BASE_URL",
        default_model=OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
    ),
    "opportunity_scout": RuntimeAgentModelSpec(
        agent_name="opportunity_scout",
        model_env="KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
        provider_env="KEYSTONE_OPPORTUNITY_SCOUT_MODEL_PROVIDER",
        base_url_env="KEYSTONE_OPPORTUNITY_SCOUT_BASE_URL",
        default_model=OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
    ),
    "outreach_composer": RuntimeAgentModelSpec(
        agent_name="outreach_composer",
        model_env="KEYSTONE_OUTREACH_COMPOSER_MODEL",
        provider_env="KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER",
        base_url_env="KEYSTONE_OUTREACH_COMPOSER_BASE_URL",
        default_model=OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
    ),
    "chief_of_staff": RuntimeAgentModelSpec(
        agent_name="chief_of_staff",
        model_env="KEYSTONE_CHIEF_OF_STAFF_MODEL",
        provider_env="KEYSTONE_CHIEF_OF_STAFF_MODEL_PROVIDER",
        base_url_env="KEYSTONE_CHIEF_OF_STAFF_BASE_URL",
        default_model=OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL,
    ),
}


class MissingOpenAIAPIKeyError(RuntimeError):
    """Raised when a caller attempts live model execution without credentials."""


class UnsupportedModelProviderError(RuntimeError):
    """Raised when an unsupported model provider is configured."""


class ModelProviderConfigurationError(RuntimeError):
    """Raised when a supported provider is missing required non-secret setup."""


class UnsafeTraceMetadataError(ValueError):
    """Raised when trace metadata may expose secrets or sensitive content."""


def mask_secret(value: str | None) -> str | None:
    """Return a log-safe representation of a secret value."""

    return MASKED_SECRET if value else None


def _metadata_key_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower())


def _validate_trace_metadata_key(key: str) -> str:
    normalized = key.strip()
    if not normalized:
        raise UnsafeTraceMetadataError("Trace metadata keys must be non-empty strings.")

    token = _metadata_key_token(normalized)
    for forbidden in FORBIDDEN_TRACE_METADATA_KEY_TERMS:
        if forbidden in token:
            raise UnsafeTraceMetadataError(
                f"Trace metadata key {normalized!r} may expose sensitive data."
            )
    return normalized


def _validate_trace_metadata_value(key: str, value: TraceMetadataValue) -> TraceMetadataValue:
    if value is None or isinstance(value, bool | int | float):
        return value
    if not isinstance(value, str):
        raise UnsafeTraceMetadataError(f"Trace metadata value for {key!r} must be a simple scalar.")

    normalized = value.strip()
    if len(normalized) > TRACE_METADATA_MAX_STRING_LENGTH:
        raise UnsafeTraceMetadataError(
            f"Trace metadata value for {key!r} is too long for safe tracing."
        )
    if "\n" in normalized or "\r" in normalized:
        raise UnsafeTraceMetadataError(
            f"Trace metadata value for {key!r} must not contain multi-line content."
        )

    lowered = normalized.lower()
    for term in SENSITIVE_TRACE_METADATA_VALUE_TERMS:
        if term in lowered:
            raise UnsafeTraceMetadataError(
                f"Trace metadata value for {key!r} may expose PHI or patient-specific data."
            )
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(normalized):
            raise UnsafeTraceMetadataError(f"Trace metadata value for {key!r} may expose a secret.")
    return normalized


def sanitize_trace_metadata(metadata: TraceMetadata | None) -> dict[str, TraceMetadataValue]:
    """Return scalar trace metadata after rejecting unsafe keys and values."""

    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise UnsafeTraceMetadataError("Trace metadata must be a mapping of simple scalars.")

    sanitized: dict[str, TraceMetadataValue] = {}
    for raw_key, raw_value in metadata.items():
        if not isinstance(raw_key, str):
            raise UnsafeTraceMetadataError("Trace metadata keys must be strings.")
        key = _validate_trace_metadata_key(raw_key)
        value = _validate_trace_metadata_value(key, raw_value)
        if value is not None:
            sanitized[key] = value
    return sanitized


@dataclass(frozen=True)
class TraceConfig:
    """RunConfig tracing fields with Keystone-safe defaults."""

    workflow_name: str = DEFAULT_WORKFLOW_NAME
    group_id: str | None = None
    trace_metadata: TraceMetadata = field(default_factory=dict)
    tracing_disabled: bool = False
    trace_include_sensitive_data: bool = False

    def __post_init__(self) -> None:
        workflow_name = self.workflow_name.strip() or DEFAULT_WORKFLOW_NAME
        group_id = self.group_id.strip() if self.group_id else None
        object.__setattr__(self, "workflow_name", workflow_name)
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "trace_metadata", sanitize_trace_metadata(self.trace_metadata))

    def with_overrides(
        self,
        *,
        workflow_name: str | None = None,
        group_id: str | None = None,
        trace_metadata: TraceMetadata | None = None,
        tracing_disabled: bool | None = None,
        trace_include_sensitive_data: bool | None = None,
    ) -> TraceConfig:
        """Return a new trace config with explicit caller overrides applied."""

        return TraceConfig(
            workflow_name=workflow_name if workflow_name is not None else self.workflow_name,
            group_id=group_id if group_id is not None else self.group_id,
            trace_metadata=(trace_metadata if trace_metadata is not None else self.trace_metadata),
            tracing_disabled=(
                tracing_disabled if tracing_disabled is not None else self.tracing_disabled
            ),
            trace_include_sensitive_data=(
                trace_include_sensitive_data
                if trace_include_sensitive_data is not None
                else self.trace_include_sensitive_data
            ),
        )


@dataclass(frozen=True)
class ModelConfig:
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str | None = None
    use_responses: bool | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    gemini_api_key: str | None = field(default=None, repr=False, compare=False)
    litellm_base_url: str | None = None

    @property
    def api_key_present(self) -> bool:
        return bool(self.api_key)

    @property
    def gemini_api_key_present(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def execution_api_key(self) -> str | None:
        if self.provider == GEMINI_PROVIDER:
            return self.gemini_api_key
        return self.api_key

    @property
    def execution_api_key_present(self) -> bool:
        return bool(self.execution_api_key)

    @property
    def masked_api_key(self) -> str | None:
        return mask_secret(self.api_key)

    def as_log_dict(self) -> dict[str, str | bool | None]:
        """Return non-secret configuration fields safe for logs."""

        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "use_responses": self.use_responses,
            "api_key": self.masked_api_key,
            "gemini_api_key": mask_secret(self.gemini_api_key),
            "litellm_base_url": self.litellm_base_url,
        }

    def openai_provider_kwargs(self) -> dict[str, str | bool | None]:
        """Return kwargs expected by the OpenAI Agents SDK provider wrapper."""

        return {
            "api_key": self.execution_api_key,
            "base_url": self.base_url,
            "use_responses": self.use_responses,
        }

    def require_live_execution_ready(self) -> None:
        self._require_pricing_metadata()
        if self.provider not in SUPPORTED_PROVIDERS:
            raise UnsupportedModelProviderError(
                f"Unsupported MODEL_PROVIDER={self.provider!r}. "
                f"Supported providers: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
            )
        if self.provider == GEMINI_PROVIDER:
            if not self.gemini_api_key_present:
                raise MissingOpenAIAPIKeyError(
                    "GEMINI_API_KEY is required for live Gemini model execution. "
                    "Agent construction and dry-run tests do not require it."
                )
            if not self.base_url:
                raise ModelProviderConfigurationError(
                    "Gemini runtime execution requires an OpenAI-compatible base URL. "
                    "Keystone normally uses the direct Gemini compatibility endpoint; "
                    "set LITELLM_BASE_URL, KEYSTONE_OPENAI_BASE_URL, or the agent "
                    "specific KEYSTONE_*_BASE_URL only when overriding it."
                )
            return
        if not self.api_key_present:
            raise MissingOpenAIAPIKeyError(
                "KEYSTONE_OPENAI_API_KEY is required for live OpenAI model execution. "
                "Agent construction and dry-run tests do not require it."
            )

    def _require_pricing_metadata(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            return
        from keystone_agents.costing import pricing_metadata_available

        if pricing_metadata_available(provider=self.provider, model=self.model):
            return
        raise ModelProviderConfigurationError(
            f"Live model execution requires checked-in pricing metadata for "
            f"{self.provider}/{self.model}. Add a reviewed row to "
            "src/keystone_agents/pricing/providers.json before testing this model, "
            "or roll back the KEYSTONE_*_MODEL/OPENAI_MODEL override to a priced "
            "model such as gpt-5.4-mini."
        )


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def openai_api_key_from_env() -> str | None:
    """Return the Keystone-specific OpenAI key."""

    return _env_value(KEYSTONE_OPENAI_API_KEY_ENV)


def _default_base_url_for_provider(provider: str, base_url: str | None) -> str | None:
    if provider == GEMINI_PROVIDER:
        return base_url or GEMINI_OPENAI_COMPAT_BASE_URL
    return base_url


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value.")


def _env_trace_metadata() -> dict[str, TraceMetadataValue]:
    value = _env_value("KEYSTONE_TRACE_METADATA")
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise UnsafeTraceMetadataError(
            "KEYSTONE_TRACE_METADATA must be a JSON object of simple scalar values."
        ) from exc
    return sanitize_trace_metadata(parsed)


def get_trace_config() -> TraceConfig:
    """Read safe SDK tracing configuration without making network or model calls."""

    return TraceConfig(
        workflow_name=_env_value("KEYSTONE_TRACE_WORKFLOW_NAME") or DEFAULT_WORKFLOW_NAME,
        group_id=_env_value("KEYSTONE_TRACE_GROUP_ID"),
        trace_metadata=_env_trace_metadata(),
        tracing_disabled=_env_bool("KEYSTONE_TRACING_DISABLED", True),
        trace_include_sensitive_data=_env_bool(
            "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
            False,
        ),
    )


def get_model_config() -> ModelConfig:
    """Read model configuration without making network or model calls."""

    provider = (_env_value("MODEL_PROVIDER") or DEFAULT_PROVIDER).lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedModelProviderError(
            f"Unsupported MODEL_PROVIDER={provider!r}. "
            f"Supported providers: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )

    litellm_base_url = _env_value("LITELLM_BASE_URL")
    base_url = _env_value(KEYSTONE_OPENAI_BASE_URL_ENV) or litellm_base_url
    resolved_base_url = _default_base_url_for_provider(provider, base_url)
    return ModelConfig(
        provider=provider,
        model=(
            _env_value(KEYSTONE_OPENAI_MODEL_ENV)
            or _env_value("KEYSTONE_DEFAULT_MODEL")
            or DEFAULT_MODEL
        ),
        base_url=resolved_base_url,
        use_responses=(
            False
            if provider in OPENAI_COMPATIBLE_GATEWAY_PROVIDERS
            or (bool(litellm_base_url) and resolved_base_url == litellm_base_url)
            else None
        ),
        api_key=openai_api_key_from_env(),
        gemini_api_key=_env_value("GEMINI_API_KEY"),
        litellm_base_url=litellm_base_url,
    )


def _runtime_agent_spec(agent_name: str | None) -> RuntimeAgentModelSpec | None:
    if not agent_name:
        return None
    normalized = agent_name.strip().lower().replace("-", "_").replace(" ", "_")
    return RUNTIME_AGENT_MODEL_SPECS.get(normalized)


def get_runtime_agent_model_config(
    agent_name: str | None,
    *,
    model_override: str | None = None,
    provider_override: str | None = None,
) -> ModelConfig:
    """Resolve model provider settings for a specific runtime agent.

    Explicit function arguments win, then agent-specific environment variables,
    then the global model provider configuration.
    """

    base = get_model_config()
    spec = _runtime_agent_spec(agent_name)
    provider_env_value = _env_value(spec.provider_env) if spec else None
    provider = (
        provider_override
        or provider_env_value
        or (spec.default_provider if spec else None)
        or base.provider
    ).lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise UnsupportedModelProviderError(
            f"Unsupported model provider {provider!r} for agent {agent_name!r}. "
            f"Supported providers: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
        )
    explicit_base_url = (_env_value(spec.base_url_env) if spec else None) or base.base_url
    base_url = _default_base_url_for_provider(provider, explicit_base_url)
    return ModelConfig(
        provider=provider,
        model=(
            model_override
            or (_env_value(spec.model_env) if spec else None)
            or (
                spec.default_model
                if spec and provider == (spec.default_provider or DEFAULT_PROVIDER)
                else None
            )
            or base.model
        ),
        base_url=base_url,
        use_responses=(
            False
            if provider in OPENAI_COMPATIBLE_GATEWAY_PROVIDERS
            or (bool(base.litellm_base_url) and base_url == base.litellm_base_url)
            else base.use_responses
        ),
        api_key=base.api_key,
        gemini_api_key=base.gemini_api_key,
        litellm_base_url=base.litellm_base_url,
    )


def get_openai_fallback_model_config(
    agent_name: str | None,
    *,
    model_override: str | None = None,
) -> ModelConfig | None:
    """Return an OpenAI fallback config when the primary provider is not OpenAI."""

    primary = get_runtime_agent_model_config(agent_name, model_override=model_override)
    if primary.provider == DEFAULT_PROVIDER:
        return None

    base = get_model_config()
    spec = _runtime_agent_spec(agent_name)
    fallback_model_env = f"KEYSTONE_{spec.agent_name.upper()}_OPENAI_FALLBACK_MODEL" if spec else ""
    fallback_base_url_env = (
        f"KEYSTONE_{spec.agent_name.upper()}_OPENAI_FALLBACK_BASE_URL" if spec else ""
    )
    return ModelConfig(
        provider=DEFAULT_PROVIDER,
        model=(
            _env_value(fallback_model_env)
            or _env_value(KEYSTONE_OPENAI_FALLBACK_MODEL_ENV)
            or _env_value(KEYSTONE_OPENAI_MODEL_ENV)
            or OPENAI_FALLBACK_DEFAULT_MODEL
        ),
        base_url=(
            _env_value(fallback_base_url_env)
            or _env_value(KEYSTONE_OPENAI_FALLBACK_BASE_URL_ENV)
            or _env_value(KEYSTONE_OPENAI_BASE_URL_ENV)
            or base.litellm_base_url
        ),
        use_responses=(
            False
            if bool(base.litellm_base_url)
            and (
                _env_value(fallback_base_url_env)
                or _env_value(KEYSTONE_OPENAI_FALLBACK_BASE_URL_ENV)
                or _env_value(KEYSTONE_OPENAI_BASE_URL_ENV)
                or base.litellm_base_url
            )
            == base.litellm_base_url
            else None
        ),
        api_key=base.api_key,
        gemini_api_key=base.gemini_api_key,
        litellm_base_url=base.litellm_base_url,
    )


def get_gemini_fallback_model_config(
    agent_name: str | None,
    *,
    model_override: str | None = None,
) -> ModelConfig | None:
    """Return an explicit Gemini fallback config when the primary provider is OpenAI."""

    primary = get_runtime_agent_model_config(agent_name, model_override=model_override)
    if primary.provider != DEFAULT_PROVIDER:
        return None
    if not _env_bool(KEYSTONE_ENABLE_GEMINI_FALLBACK_ENV, False):
        return None

    base = get_model_config()
    spec = _runtime_agent_spec(agent_name)
    fallback_model_env = f"KEYSTONE_{spec.agent_name.upper()}_GEMINI_FALLBACK_MODEL" if spec else ""
    fallback_base_url_env = (
        f"KEYSTONE_{spec.agent_name.upper()}_GEMINI_FALLBACK_BASE_URL" if spec else ""
    )
    explicit_base_url = (
        _env_value(fallback_base_url_env)
        or _env_value(KEYSTONE_GEMINI_FALLBACK_BASE_URL_ENV)
        or base.base_url
    )
    return ModelConfig(
        provider=GEMINI_PROVIDER,
        model=(
            _env_value(fallback_model_env)
            or _env_value(KEYSTONE_GEMINI_FALLBACK_MODEL_ENV)
            or GEMINI_FLASH_DEFAULT_MODEL
        ),
        base_url=_default_base_url_for_provider(GEMINI_PROVIDER, explicit_base_url),
        use_responses=False,
        api_key=base.api_key,
        gemini_api_key=base.gemini_api_key,
        litellm_base_url=base.litellm_base_url,
    )


def get_runtime_agent_model(agent_name: str | None, *, model_override: str | None = None) -> str:
    """Return the configured model name for an agent without requiring credentials."""

    return get_runtime_agent_model_config(
        agent_name,
        model_override=model_override,
    ).model
