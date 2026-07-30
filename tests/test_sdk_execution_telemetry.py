from __future__ import annotations

from typing import Any

import pytest

import keystone_agents.run as run_module
from keystone_agents.execution_telemetry import ExecutionTelemetry
from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult


class _FakeAgent:
    name = "chief_of_staff"
    model = "gpt-test"
    instructions = "Return the bounded typed result."
    tools: list[Any] = []
    model_settings = None
    output_type = ChiefOfStaffResult


def _output(summary: str) -> ChiefOfStaffResult:
    return ChiefOfStaffResult(mode="llm", summary=summary, audit_notes=[])


def test_typed_sdk_result_exposes_privacy_safe_stage_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_module,
        "run_typed_sdk_sync",
        lambda *_args, **_kwargs: ({"fake": True}, _output("Done.")),
    )
    monkeypatch.setattr(
        run_module,
        "enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=_FakeAgent(),
        typed_input={"request": "private request body"},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    telemetry = ExecutionTelemetry.model_validate(result.execution_telemetry)
    summary = telemetry.summary()

    assert telemetry.status == "completed"
    assert summary.final_response_ms is not None
    assert [span.stage for span in telemetry.spans] == [
        "sdk.model_attempt",
        "sdk.budget_check",
    ]
    assert "private request body" not in str(result.execution_telemetry)


def test_typed_sdk_telemetry_preserves_retry_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "1")
    calls = 0

    class _RateLimitError(Exception):
        status_code = 429

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _RateLimitError("retry")
        return {"fake": True}, _output("Recovered.")

    monkeypatch.setattr(run_module, "run_typed_sdk_sync", fake_run)
    monkeypatch.setattr(run_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        run_module,
        "enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=_FakeAgent(),
        typed_input={"request": "bounded read"},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    telemetry = ExecutionTelemetry.model_validate(result.execution_telemetry)
    attempts = [
        span for span in telemetry.spans if span.stage == "sdk.model_attempt"
    ]

    assert [(span.attempt_index, span.status) for span in attempts] == [
        (1, "error"),
        (2, "ok"),
    ]
    assert result.request_cache["rate_limit_retries"] == 1
