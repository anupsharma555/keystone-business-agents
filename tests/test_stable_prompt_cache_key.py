from __future__ import annotations

from dataclasses import replace
from typing import Any

from keystone_agents.agents.manual_request_planner import (
    ManualRequestPlannerInput,
    build_manual_request_planner_agent,
)
from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import (
    agent_with_stable_prompt_cache_key,
    prompt_cache_key_audit_metadata,
)


def _explicit_key(agent: Any) -> str:
    settings = agent.model_settings
    return str((settings.extra_args or {}).get("prompt_cache_key") or "")


def test_static_prompt_cache_key_is_stable_across_asks_threads_and_followups(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "operator-fixture")
    base = build_manual_request_planner_agent(model="gpt-5.4-mini")

    first = agent_with_stable_prompt_cache_key(
        base,
        provider="openai",
        model_name="gpt-5.4-mini",
    )
    second = agent_with_stable_prompt_cache_key(
        build_manual_request_planner_agent(model="gpt-5.4-mini"),
        provider="openai",
        model_name="gpt-5.4-mini",
    )

    assert _explicit_key(first)
    assert _explicit_key(first) == _explicit_key(second)
    assert "operator-fixture" not in _explicit_key(first)
    assert prompt_cache_key_audit_metadata(first)["prompt_cache_key_source"] == (
        "explicit_static_profile"
    )


def test_static_prompt_cache_key_invalidates_on_profile_changes(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "operator-a")
    base = build_manual_request_planner_agent(model="gpt-5.4-mini")
    first = agent_with_stable_prompt_cache_key(
        base,
        provider="openai",
        model_name="gpt-5.4-mini",
    )
    changed_model = agent_with_stable_prompt_cache_key(
        base,
        provider="openai",
        model_name="gpt-5.4",
    )
    changed_prompt = agent_with_stable_prompt_cache_key(
        base.clone(instructions=str(base.instructions) + "\nProfile revision."),
        provider="openai",
        model_name="gpt-5.4-mini",
    )
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "operator-b")
    changed_scope = agent_with_stable_prompt_cache_key(
        base,
        provider="openai",
        model_name="gpt-5.4-mini",
    )

    assert (
        len(
            {
                _explicit_key(first),
                _explicit_key(changed_model),
                _explicit_key(changed_prompt),
                _explicit_key(changed_scope),
            }
        )
        == 4
    )


def test_static_prompt_cache_key_is_openai_only_and_can_be_disabled(
    monkeypatch,
) -> None:
    base = build_manual_request_planner_agent(model="gpt-5.4-mini")

    gemini = agent_with_stable_prompt_cache_key(
        base,
        provider="gemini",
        model_name="gemini-2.5-flash",
    )
    assert not _explicit_key(gemini)

    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "off")
    disabled = agent_with_stable_prompt_cache_key(
        base,
        provider="openai",
        model_name="gpt-5.4-mini",
    )
    assert not _explicit_key(disabled)


def test_explicit_caller_prompt_cache_key_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "operator-fixture")
    base = build_manual_request_planner_agent(model="gpt-5.4-mini")
    supplied = base.clone(
        model_settings=replace(
            base.model_settings,
            extra_args={"prompt_cache_key": "caller-owned-cache-key"},
        )
    )

    resolved = agent_with_stable_prompt_cache_key(
        supplied,
        provider="openai",
        model_name="gpt-5.4-mini",
    )

    assert _explicit_key(resolved) == "caller-owned-cache-key"


def test_sdk_run_reports_explicit_profile_cache_key(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "operator-fixture")

    class RawResult:
        usage = {
            "requests": 1,
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 120,
        }

    def fake_run_typed_sdk_sync(
        agent: Any,
        *_args: Any,
        **_kwargs: Any,
    ) -> tuple[RawResult, ManualRequestPlan]:
        assert _explicit_key(agent)
        return RawResult(), ManualRequestPlan(
            source="llm",
            target_agent="chief_of_staff",
            intent="route_request",
        )

    monkeypatch.setattr(
        "keystone_agents.run.run_typed_sdk_sync",
        fake_run_typed_sdk_sync,
    )

    result = run_typed_sdk_agent(
        agent=build_manual_request_planner_agent(model="gpt-5.4-mini"),
        typed_input=ManualRequestPlannerInput(request_text="Summarize the status."),
        output_type=ManualRequestPlan,
        run_config=object(),
        config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
    )

    assert result.request_cache["prompt_cache_key_present"] is True
    assert result.request_cache["prompt_cache_key_source"] == "explicit_static_profile"
    assert result.usage["prompt_cache_key_present"] is True
    assert result.usage["prompt_cache_key_hash"] == result.request_cache["prompt_cache_key_hash"]
