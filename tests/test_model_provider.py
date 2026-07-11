from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import keystone_agents.run as agent_run
import keystone_agents.sdk as sdk
from keystone_agents.config import load_settings
from keystone_agents.model_provider import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_WORKFLOW_NAME,
    GEMINI_OPENAI_COMPAT_BASE_URL,
    MASKED_SECRET,
    OPENAI_BUSINESS_AGENT_DEFAULT_MODEL,
    OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL,
    OPENAI_ORCHESTRATOR_DEFAULT_MODEL,
    MissingOpenAIAPIKeyError,
    ModelConfig,
    ModelProviderConfigurationError,
    TraceConfig,
    UnsafeTraceMetadataError,
    UnsupportedModelProviderError,
    get_gemini_fallback_model_config,
    get_runtime_agent_model_config,
    get_trace_config,
    mask_secret,
)
from keystone_agents.sdk import (
    build_live_run_config,
    build_local_run_config,
    build_sdk_agent,
    create_agent,
    get_model_config,
    load_prompt,
    validate_sdk_available,
)


class MinimalOutput(BaseModel):
    value: str


PROJECT_ROOT = Path(__file__).resolve().parents[1]


MODEL_ENV_VARS = (
    "MODEL_PROVIDER",
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "KEYSTONE_OPENAI_MODEL",
    "GEMINI_API_KEY",
    "LITELLM_BASE_URL",
    "KEYSTONE_DEFAULT_MODEL",
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
    "KEYSTONE_ENABLE_GEMINI_FALLBACK",
    "KEYSTONE_GEMINI_FALLBACK_MODEL",
    "KEYSTONE_GEMINI_FALLBACK_BASE_URL",
    "KEYSTONE_CHIEF_OF_STAFF_MODEL",
    "KEYSTONE_CHIEF_OF_STAFF_MODEL_PROVIDER",
    "KEYSTONE_CHIEF_OF_STAFF_BASE_URL",
    "KEYSTONE_TRACE_WORKFLOW_NAME",
    "KEYSTONE_TRACE_GROUP_ID",
    "KEYSTONE_TRACE_METADATA",
    "KEYSTONE_TRACING_DISABLED",
    "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
)


def clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_get_model_config_defaults_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)

    config = get_model_config()

    assert config.provider == DEFAULT_PROVIDER
    assert DEFAULT_MODEL == "gpt-5.4-mini"
    assert config.model == DEFAULT_MODEL
    assert config.base_url is None
    assert config.api_key_present is False


def test_validate_sdk_available() -> None:
    assert validate_sdk_available() is True


def test_typed_sdk_sync_forwards_session_to_local_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    session = object()

    class DummyRunner:
        @staticmethod
        def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
            calls.append({"agent": agent, "prompt": prompt, **kwargs})
            return SimpleNamespace(final_output={"value": "ok"})

    monkeypatch.setattr(sdk, "Runner", DummyRunner)
    monkeypatch.setattr(sdk, "validate_sdk_available", lambda: True)

    agent = SimpleNamespace(name="Dummy Agent", model=None)
    raw_result, output = sdk.run_typed_sdk_sync(
        agent,
        "Hello",
        MinimalOutput,
        run_config=SimpleNamespace(model="fake-local"),
        session=session,
    )

    assert raw_result.final_output == {"value": "ok"}
    assert output == MinimalOutput(value="ok")
    assert calls[0]["session"] is session


def test_typed_sdk_sync_forwards_session_to_live_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    session = object()
    close_calls = 0

    class FakeAsyncClient:
        def is_closed(self) -> bool:
            return close_calls > 0

        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1

    class DummyRunner:
        @staticmethod
        def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
            calls.append({"agent": agent, "prompt": prompt, **kwargs})
            return SimpleNamespace(final_output={"value": "ok"})

    monkeypatch.setattr(sdk, "Runner", DummyRunner)
    monkeypatch.setattr(
        sdk,
        "_live_config_for_agent",
        lambda agent, config: SimpleNamespace(provider="openai", model="fake-live"),
    )
    monkeypatch.setattr(
        sdk,
        "build_live_run_config",
        lambda *args, **kwargs: SimpleNamespace(
            model="fake-live",
            model_provider=SimpleNamespace(_client=FakeAsyncClient()),
        ),
    )

    sdk.run_typed_sdk_sync(
        SimpleNamespace(name="Dummy Agent", model=None),
        "Hello",
        MinimalOutput,
        live=True,
        session=session,
    )

    assert calls[0]["session"] is session
    assert close_calls == 1


def test_typed_sdk_sync_forwards_max_turns_to_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class DummyRunner:
        @staticmethod
        def run_sync(agent: Any, prompt: str, **kwargs: Any) -> Any:
            calls.append({"agent": agent, "prompt": prompt, **kwargs})
            return SimpleNamespace(final_output={"value": "ok"})

    monkeypatch.setattr(sdk, "Runner", DummyRunner)
    monkeypatch.setattr(sdk, "validate_sdk_available", lambda: True)

    sdk.run_typed_sdk_sync(
        SimpleNamespace(name="Dummy Agent", model=None),
        "Hello",
        MinimalOutput,
        run_config=SimpleNamespace(model="fake-local"),
        max_turns=7,
    )

    assert calls[0]["max_turns"] == 7


def test_retrieved_sdk_synthesis_forwards_session(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    session = object()

    def fake_run_typed_sdk_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        output = MinimalOutput(value="ok")
        return agent_run.TypedAgentRunResult(
            agent_name=kwargs["agent"].name,
            output=output,
            raw_result=SimpleNamespace(final_output=output),
            live=False,
        )

    monkeypatch.setattr(agent_run, "run_typed_sdk_agent", fake_run_typed_sdk_agent)

    outcome = agent_run.run_retrieved_sdk_synthesis(
        agent=SimpleNamespace(name="Dummy Agent", model=None),
        output_type=MinimalOutput,
        retrieve=lambda: {"raw": True},
        normalize=lambda raw: f"raw={raw['raw']}",
        input_summary="dummy",
        run_config=SimpleNamespace(model="fake-local"),
        session=session,
    )

    assert outcome.output == MinimalOutput(value="ok")
    assert captured["session"] is session


def test_typed_sdk_agent_uses_env_session_when_not_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    session = object()

    def fake_run_typed_sdk_sync(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(final_output={"value": "ok"}), MinimalOutput(value="ok")

    monkeypatch.setattr(agent_run, "build_sdk_session_from_env", lambda: session)
    monkeypatch.setattr(agent_run, "run_typed_sdk_sync", fake_run_typed_sdk_sync)

    result = agent_run.run_typed_sdk_agent(
        agent=SimpleNamespace(name="Dummy Agent", model=None),
        typed_input="Hello",
        output_type=MinimalOutput,
        run_config=SimpleNamespace(model="fake-local"),
    )

    assert result.output == MinimalOutput(value="ok")
    assert captured["session"] is session


def test_sdk_fallback_validation_errors_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdk, "_SDK_IMPORT_ERROR", ImportError("missing agents sdk"))
    monkeypatch.setattr(sdk, "_SANDBOX_IMPORT_ERROR", ImportError("missing sandbox sdk"))

    with pytest.raises(RuntimeError, match="OpenAI Agents SDK is not installed"):
        sdk.validate_sdk_available()
    with pytest.raises(RuntimeError, match="sandbox classes are unavailable"):
        sdk.validate_sandbox_sdk_available()


def test_model_config_supports_openai_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "unit-test-openai-key")
    monkeypatch.setenv("KEYSTONE_OPENAI_BASE_URL", "https://openai-compatible.example/v1")
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "test-model")

    config = get_model_config()

    assert config.model == "test-model"
    assert config.base_url == "https://openai-compatible.example/v1"
    assert config.api_key_present is True
    assert "unit-test-openai-key" not in repr(config)


def test_keystone_openai_api_key_ignores_standard_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "openclaw-shell-key")
    monkeypatch.setenv("OPENAI_MODEL", "openclaw-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "openclaw.local/v1")

    openclaw_only = get_model_config()

    assert openclaw_only.api_key_present is False
    assert openclaw_only.model == DEFAULT_MODEL
    assert openclaw_only.base_url is None

    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "keystone-unit-test-key")
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "keystone-model")
    monkeypatch.setenv("KEYSTONE_OPENAI_BASE_URL", "https://keystone.example/v1")

    config = get_model_config()
    settings = load_settings()

    assert config.api_key_present is True
    assert config.model == "keystone-model"
    assert config.base_url == "https://keystone.example/v1"
    assert config.openai_provider_kwargs()["api_key"] == "keystone-unit-test-key"
    assert settings.openai_api_key == "keystone-unit-test-key"


def test_litellm_base_url_can_back_openai_compatible_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000/v1")

    config = get_model_config()

    assert config.base_url == "http://localhost:4000/v1"
    assert config.litellm_base_url == "http://localhost:4000/v1"
    assert config.use_responses is False


def test_litellm_gateway_mode_does_not_add_python_litellm_dependency() -> None:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["dependencies"]

    assert not any(dependency.lower().startswith("litellm") for dependency in dependencies)


def test_live_model_config_rejects_unknown_pricing_alias_before_execution() -> None:
    config = ModelConfig(provider="openai", model="gpt-5.5", api_key="unit-test-key")

    with pytest.raises(ModelProviderConfigurationError, match="openai/gpt-5\\.5"):
        config.require_live_execution_ready()


def test_settings_expose_model_provider_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("KEYSTONE_OPENAI_BASE_URL", "https://openai-compatible.example/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000/v1")

    settings = load_settings()

    assert settings.model_provider == "openai"
    assert settings.default_model == "gpt-5.4"
    assert settings.openai_model == "gpt-5.4"
    assert settings.openai_base_url == "https://openai-compatible.example/v1"
    assert settings.gemini_api_key == "unit-test-gemini-key"
    assert settings.litellm_base_url == "http://localhost:4000/v1"


def test_trace_config_defaults_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)

    trace_config = get_trace_config()
    settings = load_settings()

    assert trace_config.workflow_name == DEFAULT_WORKFLOW_NAME
    assert trace_config.group_id is None
    assert trace_config.trace_metadata == {}
    assert trace_config.tracing_disabled is True
    assert trace_config.trace_include_sensitive_data is False
    assert settings.workflow_name == DEFAULT_WORKFLOW_NAME
    assert settings.group_id is None
    assert settings.trace_metadata is None
    assert settings.tracing_disabled is True
    assert settings.trace_include_sensitive_data is False


def test_trace_config_supports_safe_environment_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_TRACE_WORKFLOW_NAME", "Keystone unit workflow")
    monkeypatch.setenv("KEYSTONE_TRACE_GROUP_ID", "unit-group-1")
    monkeypatch.setenv(
        "KEYSTONE_TRACE_METADATA",
        '{"agent_name":"gmail_triage","run_type":"live_review","attempt":1}',
    )
    monkeypatch.setenv("KEYSTONE_TRACING_DISABLED", "true")
    monkeypatch.setenv("KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA", "false")

    trace_config = get_trace_config()
    settings = load_settings()

    assert trace_config.workflow_name == "Keystone unit workflow"
    assert trace_config.group_id == "unit-group-1"
    assert trace_config.trace_metadata == {
        "agent_name": "gmail_triage",
        "run_type": "live_review",
        "attempt": 1,
    }
    assert trace_config.tracing_disabled is True
    assert trace_config.trace_include_sensitive_data is False
    assert settings.trace_metadata == trace_config.trace_metadata


def test_legacy_model_env_vars_remain_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "gpt-5.4")

    config = get_model_config()
    settings = load_settings()

    assert config.model == "gpt-5.4"
    assert settings.default_model == "gpt-5.4"
    assert settings.openai_model == "gpt-5.4"


def test_runtime_agent_model_config_supports_gemini_for_gmail_and_outreach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_BASE_URL", "http://localhost:4000/v1")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_BASE_URL", "http://localhost:4000/v1")

    gmail_config = get_runtime_agent_model_config("gmail_triage")
    outreach_config = get_runtime_agent_model_config("outreach_composer")
    settings = load_settings()

    assert gmail_config.provider == "gemini"
    assert gmail_config.model == "gemini-2.5-flash"
    assert gmail_config.base_url == "http://localhost:4000/v1"
    assert gmail_config.openai_provider_kwargs() == {
        "api_key": "unit-test-gemini-key",
        "base_url": "http://localhost:4000/v1",
        "use_responses": False,
    }
    gmail_config.require_live_execution_ready()
    assert outreach_config.provider == "gemini"
    assert outreach_config.model == "gemini-2.5-flash-lite"
    assert settings.runtime_agent_models["gmail_triage"]["provider"] == "gemini"
    assert settings.runtime_agent_models["outreach_composer"]["model"] == (
        "gemini-2.5-flash-lite"
    )


def test_gemini_runtime_uses_direct_openai_compatible_endpoint_when_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL", "gemini-2.5-flash")

    gmail_config = get_runtime_agent_model_config("gmail_triage")
    outreach_config = get_runtime_agent_model_config("outreach_composer")

    assert gmail_config.provider == "gemini"
    assert gmail_config.model == "gemini-2.5-flash"
    assert gmail_config.base_url == GEMINI_OPENAI_COMPAT_BASE_URL
    assert gmail_config.openai_provider_kwargs() == {
        "api_key": "unit-test-gemini-key",
        "base_url": GEMINI_OPENAI_COMPAT_BASE_URL,
        "use_responses": False,
    }
    gmail_config.require_live_execution_ready()
    assert outreach_config.provider == "gemini"
    assert outreach_config.model == "gemini-2.5-flash"
    assert outreach_config.base_url == GEMINI_OPENAI_COMPAT_BASE_URL
    assert outreach_config.openai_provider_kwargs() == {
        "api_key": "unit-test-gemini-key",
        "base_url": GEMINI_OPENAI_COMPAT_BASE_URL,
        "use_responses": False,
    }
    outreach_config.require_live_execution_ready()


def test_gemini_fallback_is_explicit_backup_for_openai_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_ENABLE_GEMINI_FALLBACK", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")

    fallback = get_gemini_fallback_model_config("gmail_triage")

    assert fallback is not None
    assert fallback.provider == "gemini"
    assert fallback.model == "gemini-2.5-flash"
    assert fallback.base_url == GEMINI_OPENAI_COMPAT_BASE_URL
    fallback.require_live_execution_ready()


def test_gemini_fallback_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")

    assert get_gemini_fallback_model_config("gmail_triage") is None


def test_runtime_agent_model_config_keeps_research_agents_openai_based(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "openai-default-fixture")
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_MODEL", "openai-orchestrator-fixture")
    monkeypatch.setenv("KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL", "openai-account-fixture")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_SCOUT_MODEL", "openai-opportunity-fixture")
    monkeypatch.setenv("KEYSTONE_CHIEF_OF_STAFF_MODEL", "openai-chief-fixture")

    assert get_runtime_agent_model_config("orchestrator").provider == "openai"
    assert get_runtime_agent_model_config("orchestrator").model == ("openai-orchestrator-fixture")
    assert get_runtime_agent_model_config("business_research_analyst").model == (
        "openai-account-fixture"
    )
    assert get_runtime_agent_model_config("opportunity_scout").model == (
        "openai-opportunity-fixture"
    )
    assert get_runtime_agent_model_config("chief_of_staff").model == "openai-chief-fixture"


def test_business_agent_model_defaults_are_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)

    assert OPENAI_ORCHESTRATOR_DEFAULT_MODEL == "gpt-5.4-mini"
    assert get_runtime_agent_model_config("orchestrator").model == (
        OPENAI_ORCHESTRATOR_DEFAULT_MODEL
    )
    gmail = get_runtime_agent_model_config("gmail_triage")
    outreach = get_runtime_agent_model_config("outreach_composer")
    assert OPENAI_BUSINESS_AGENT_DEFAULT_MODEL == "gpt-5.4-mini"
    assert gmail.provider == "openai"
    assert gmail.model == OPENAI_BUSINESS_AGENT_DEFAULT_MODEL
    assert outreach.provider == "openai"
    assert outreach.model == OPENAI_BUSINESS_AGENT_DEFAULT_MODEL
    assert get_runtime_agent_model_config("business_research_analyst").model == (
        OPENAI_BUSINESS_AGENT_DEFAULT_MODEL
    )
    assert get_runtime_agent_model_config("opportunity_scout").model == (
        OPENAI_BUSINESS_AGENT_DEFAULT_MODEL
    )
    assert OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL == "gpt-5.4-mini"
    assert get_runtime_agent_model_config("chief_of_staff").model == (
        OPENAI_CHIEF_OF_STAFF_DEFAULT_MODEL
    )


def test_gmail_triage_explicit_openai_uses_operating_agent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "openai")

    config = get_runtime_agent_model_config("gmail_triage")

    assert config.provider == "openai"
    assert config.model == OPENAI_BUSINESS_AGENT_DEFAULT_MODEL


def test_outreach_composer_explicit_openai_uses_operating_agent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER", "openai")

    config = get_runtime_agent_model_config("outreach_composer")

    assert config.provider == "openai"
    assert config.model == OPENAI_BUSINESS_AGENT_DEFAULT_MODEL


def test_gemini_runtime_keeps_litellm_as_optional_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://localhost:4000/v1")

    config = get_runtime_agent_model_config("gmail_triage")

    assert config.base_url == "http://localhost:4000/v1"
    assert config.openai_provider_kwargs() == {
        "api_key": "unit-test-gemini-key",
        "base_url": "http://localhost:4000/v1",
        "use_responses": False,
    }
    config.require_live_execution_ready()


def test_secrets_are_masked_for_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "unit-test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-gemini-key")

    config = get_model_config()
    log_payload = config.as_log_dict()

    assert mask_secret("unit-test-openai-key") == MASKED_SECRET
    assert log_payload["api_key"] == MASKED_SECRET
    assert log_payload["gemini_api_key"] == MASKED_SECRET
    assert "unit-test-openai-key" not in repr(config)
    assert "unit-test-openai-key" not in str(log_payload)
    assert "unit-test-gemini-key" not in str(log_payload)


def test_live_run_config_sets_safe_trace_defaults_without_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    config = get_model_config()
    config = config.__class__(model=config.model, api_key="unit-test-openai-key")

    run_config = build_live_run_config(config)

    assert run_config.model == DEFAULT_MODEL
    assert run_config.workflow_name == DEFAULT_WORKFLOW_NAME
    assert run_config.group_id is None
    assert run_config.trace_metadata is None
    assert run_config.tracing_disabled is True
    assert run_config.trace_include_sensitive_data is False


def test_private_context_profile_forces_nonpersistent_model_and_trace_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    config = get_model_config()
    config = config.__class__(model=config.model, api_key="unit-test-openai-key")

    with sdk.private_context_sdk_profile():
        model_settings = sdk.build_model_settings()
        run_config = build_live_run_config(
            config,
            tracing_disabled=False,
            trace_include_sensitive_data=True,
        )

    assert model_settings.store is False
    assert model_settings.prompt_cache_retention == "in_memory"
    assert run_config.tracing_disabled is True
    assert run_config.trace_include_sensitive_data is False
    assert sdk.active_sdk_data_handling_profile() is None


def test_live_run_config_uses_keystone_key_for_enabled_trace_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-project-key")
    config = get_model_config()
    config = config.__class__(model=config.model, api_key="unit-test-keystone-key")
    exported_keys: list[str] = []
    monkeypatch.setattr(sdk, "set_tracing_export_api_key", exported_keys.append)

    run_config = build_live_run_config(config, tracing_disabled=False)

    assert run_config.tracing_disabled is False
    assert exported_keys == ["unit-test-keystone-key"]


def test_live_run_config_exposes_safe_trace_metadata_without_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    config = get_model_config()
    config = config.__class__(
        model="gpt-5.4-mini-2026-06-01",
        api_key="unit-test-openai-key",
    )

    run_config = build_live_run_config(
        config,
        workflow_name="Keystone unit workflow",
        group_id="unit-group-1",
        trace_metadata={
            "agent_name": "gmail_triage",
            "run_type": "live_review",
            "attempt": 1,
            "dry_run": False,
        },
        tracing_disabled=True,
        trace_include_sensitive_data=True,
    )

    assert run_config.model == "gpt-5.4-mini-2026-06-01"
    assert run_config.workflow_name == "Keystone unit workflow"
    assert run_config.group_id == "unit-group-1"
    assert run_config.trace_metadata == {
        "agent_name": "gmail_triage",
        "run_type": "live_review",
        "attempt": "1",
        "dry_run": "false",
    }
    assert run_config.tracing_disabled is True
    assert run_config.trace_include_sensitive_data is True


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_key": "unit-test-openai-key"},
        {"email_body": "Hello from a full email body."},
        {"draft_body": "Draft outbound copy."},
        {"prompt": "Classify this email."},
        {"note": "patient diagnosis details"},
        {"run_name": "sk-" + "abcdefghijklmnopqrstuvwxyz"},
        {"run_name": "line one\nline two"},
        {"run_name": "x" * 201},
    ],
)
def test_trace_metadata_rejects_sensitive_content(
    metadata: dict[str, object],
) -> None:
    with pytest.raises(UnsafeTraceMetadataError):
        TraceConfig(trace_metadata=metadata)  # type: ignore[arg-type]


def test_agent_construction_does_not_require_openai_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)

    agent = build_sdk_agent(
        name="test_agent",
        instructions="Return a structured test result.",
        output_type=MinimalOutput,
    )

    assert agent.name == "test_agent"
    assert agent.output_type is MinimalOutput


def test_sdk_agent_local_fallback_preserves_direct_call_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setattr(sdk, "_SDK_IMPORT_ERROR", ImportError("missing agents sdk"))

    def direct_tool() -> str:
        return "ok"

    input_guardrail = object()
    output_guardrail = object()

    agent = sdk.build_sdk_agent(
        name="fallback_agent",
        instructions="Return a structured test result.",
        output_type=MinimalOutput,
        tools=[direct_tool],
        handoffs=["handoff"],
        guardrails={"input": [input_guardrail], "output": [output_guardrail]},
        model="fixture-model",
        handoff_description="Use for local fallback tests.",
    )

    assert isinstance(agent, sdk.LocalAgent)
    assert agent.name == "fallback_agent"
    assert agent.handoff_description == "Use for local fallback tests."
    assert agent.model == "fixture-model"
    assert agent.tools == [direct_tool]
    assert agent.handoffs == ["handoff"]
    assert agent.input_guardrails == [input_guardrail]
    assert agent.output_guardrails == [output_guardrail]


def test_create_agent_alias_uses_sdk_agent_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)

    agent = create_agent(
        name="alias_agent",
        instructions="Return a structured test result.",
        output_type=MinimalOutput,
        model="fixture-model",
    )

    assert agent.name == "alias_agent"
    assert agent.output_type is MinimalOutput


def test_missing_api_key_error_is_clear_only_for_live_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_model_env(monkeypatch)

    config = get_model_config()

    with pytest.raises(MissingOpenAIAPIKeyError, match="KEYSTONE_OPENAI_API_KEY is required"):
        config.require_live_execution_ready()

    with pytest.raises(MissingOpenAIAPIKeyError, match="KEYSTONE_OPENAI_API_KEY is required"):
        build_live_run_config(config)


def test_unsupported_provider_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")

    with pytest.raises(
        UnsupportedModelProviderError,
        match="Unsupported MODEL_PROVIDER='anthropic'",
    ):
        get_model_config()


def test_prompt_loading_works_from_markdown_files() -> None:
    safety_prompt = load_prompt("safety_policy")
    explicit_safety_prompt = load_prompt("safety_policy.md")

    assert "Safety Policy" in safety_prompt
    assert explicit_safety_prompt == safety_prompt


def test_prompt_loading_rejects_paths_and_non_markdown_names() -> None:
    with pytest.raises(ValueError, match="not a path"):
        load_prompt("../safety_policy")
    with pytest.raises(ValueError, match="markdown file"):
        load_prompt("safety_policy.txt")


def test_local_tool_guardrail_fallbacks_cover_async_and_exception_paths() -> None:
    async def async_reject(_data: object) -> SimpleNamespace:
        return SimpleNamespace(behavior={"type": "raise_exception"})

    guardrail = sdk.LocalToolInputGuardrail(async_reject)
    named_guardrail = sdk.LocalToolOutputGuardrail(async_reject, name="named_output_guardrail")

    assert guardrail.get_name() == "async_reject"
    assert named_guardrail.get_name() == "named_output_guardrail"

    with pytest.raises(sdk.ToolGuardrailViolation, match="Tool guardrail rejected"):
        sdk.enforce_local_tool_input_guardrails(
            tool_name="test_tool",
            payload={"value": "unsafe"},
            guardrails=[guardrail],
        )


def test_local_tool_payload_falls_back_when_signature_is_unavailable() -> None:
    class BadSignatureCallable:
        @property
        def __signature__(self) -> object:
            raise ValueError("signature unavailable")

        def __call__(self, value: str) -> str:
            return value

    payload = sdk._tool_payload(  # noqa: SLF001
        BadSignatureCallable(),
        ("positional",),
        {"keyword": "value"},
    )

    assert payload == {"args": ("positional",), "kwargs": {"keyword": "value"}}


def test_local_run_config_preserves_safe_trace_metadata() -> None:
    run_config = build_local_run_config(
        model_provider=object(),
        model="fixture-model",
        workflow_name="Keystone local branch test",
        group_id="local-group",
        trace_metadata={"agent_name": "unit_test"},
        trace_include_sensitive_data=False,
    )

    assert run_config.model == "fixture-model"
    assert run_config.workflow_name == "Keystone local branch test"
    assert run_config.group_id == "local-group"
    assert run_config.trace_metadata == {"agent_name": "unit_test"}


def test_run_sdk_sync_delegates_to_live_config_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_build_live_run_config(*args: object, **kwargs: object) -> str:
        calls["build_args"] = args
        calls["build_kwargs"] = kwargs
        return "run-config"

    class FakeRunner:
        @staticmethod
        def run_sync(agent: object, prompt: str, *, run_config: object) -> dict[str, object]:
            return {"agent": agent, "prompt": prompt, "run_config": run_config}

    agent = object()
    monkeypatch.setattr(sdk, "build_live_run_config", fake_build_live_run_config)
    monkeypatch.setattr(sdk, "Runner", FakeRunner)

    result = sdk.run_sdk_sync(
        agent,
        "Prompt",
        workflow_name="workflow",
        trace_metadata={"agent_name": "unit_test"},
    )

    assert result == {"agent": agent, "prompt": "Prompt", "run_config": "run-config"}
    assert calls["build_kwargs"] == {
        "workflow_name": "workflow",
        "group_id": None,
        "trace_metadata": {"agent_name": "unit_test"},
        "tracing_disabled": None,
        "trace_include_sensitive_data": None,
        "trace_config": None,
    }
