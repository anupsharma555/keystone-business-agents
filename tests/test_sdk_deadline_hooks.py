from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import keystone_agents.sdk as sdk
from keystone_agents.runtime.execution_deadline import (
    ExecutionDeadlineExceeded,
    ExecutionDeadlineLedger,
    activate_execution_deadline,
)
from keystone_agents.runtime.request_budget import (
    ModelRequestBudgetExhausted,
    activate_model_request_budget,
)


class _FakeClock:
    def __init__(self) -> None:
        self.wall_ms = 1_800_000_000_000
        self.monotonic_ms = 25_000

    def wall(self) -> int:
        return self.wall_ms

    def monotonic(self) -> int:
        return self.monotonic_ms

    def advance(self, milliseconds: int) -> None:
        self.wall_ms += milliseconds
        self.monotonic_ms += milliseconds


def _deadline(clock: _FakeClock, *, soft_seconds: float = 10) -> ExecutionDeadlineLedger:
    return ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=soft_seconds,
        hard_timeout_seconds=soft_seconds + 5,
        finalization_reserve_seconds=2,
        correlation_id="sdk-hook-test",
        wall_clock_ms=clock.wall,
        monotonic_clock_ms=clock.monotonic,
    )


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_deadline_rejects_llm_before_model_request_budget_is_consumed() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock, soft_seconds=0)
    agent = SimpleNamespace(name="Gmail Triage")

    with activate_execution_deadline(deadline), activate_model_request_budget(2) as budget:
        hooks = sdk.execution_boundary_hooks()
        assert hooks is not None
        with pytest.raises(ExecutionDeadlineExceeded):
            _run(hooks.on_llm_start(None, agent, None, []))
        budget_snapshot = budget.snapshot()

    assert budget_snapshot["consumed"] == 0
    assert deadline.snapshot()["events"][0]["phase"] == "rejected"


def test_existing_model_request_budget_still_blocks_request_n_plus_one() -> None:
    agent = SimpleNamespace(name="Chief of Staff")

    with activate_model_request_budget(1) as budget:
        hooks = sdk.model_request_budget_hooks()
        assert hooks is not None
        _run(hooks.on_llm_start(None, agent, None, []))
        with pytest.raises(ModelRequestBudgetExhausted):
            _run(hooks.on_llm_start(None, agent, None, []))
        snapshot = budget.snapshot()

    assert snapshot["consumed"] == 1
    assert snapshot["exhaustion_stage"] == "Chief of Staff:llm_start"


def test_hook_records_completed_llm_then_stops_only_the_next_boundary() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock, soft_seconds=5)
    agent = SimpleNamespace(name="Opportunity Scout")

    with activate_execution_deadline(deadline):
        hooks = sdk.execution_boundary_hooks()
        assert hooks is not None
        _run(hooks.on_llm_start(None, agent, None, []))
        clock.advance(6_000)
        # Lifecycle hooks observe completion; they do not preempt the in-flight call.
        _run(hooks.on_llm_end(None, agent, SimpleNamespace()))
        with pytest.raises(ExecutionDeadlineExceeded):
            _run(hooks.on_llm_start(None, agent, None, []))

    events = deadline.snapshot()["events"]
    assert [event["phase"] for event in events] == [
        "admitted",
        "completed",
        "rejected",
    ]
    assert events[1]["remaining_soft_ms"] == 0


def test_hook_checks_tool_boundary_and_records_only_safe_names() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock, soft_seconds=2)
    agent = SimpleNamespace(name="Business Research Analyst")
    tool = SimpleNamespace(name="search_web")

    with activate_execution_deadline(deadline):
        hooks = sdk.execution_boundary_hooks()
        assert hooks is not None
        _run(hooks.on_tool_start(None, agent, tool))
        clock.advance(3_000)
        _run(hooks.on_tool_end(None, agent, tool, {"private": "not recorded"}))
        with pytest.raises(ExecutionDeadlineExceeded):
            _run(hooks.on_tool_start(None, agent, tool))

    events = deadline.snapshot()["events"]
    assert [event["phase"] for event in events] == [
        "admitted",
        "completed",
        "rejected",
    ]
    assert all("private" not in str(event) for event in events)
    assert events[0]["stage"] == "Business_Research_Analyst:tool:search_web"


def test_request_budget_rejection_is_checkpointed_without_provider_dispatch() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)
    agent = SimpleNamespace(name="RSS Context")

    with activate_execution_deadline(deadline), activate_model_request_budget(0):
        hooks = sdk.execution_boundary_hooks()
        assert hooks is not None
        with pytest.raises(ModelRequestBudgetExhausted):
            _run(hooks.on_llm_start(None, agent, None, []))

    assert [event["phase"] for event in deadline.snapshot()["events"]] == [
        "admitted",
        "cancelled_before_dispatch",
    ]


def test_sync_runner_receives_composite_hooks_when_only_deadline_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)
    captured: dict[str, Any] = {}

    def fake_run_sync(_agent: Any, _prompt: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(sdk.Runner, "run_sync", fake_run_sync)
    with activate_execution_deadline(deadline):
        result = sdk._run_sync_with_optional_session(
            SimpleNamespace(name="test"),
            "bounded request",
            run_config=SimpleNamespace(),
        )

    assert result == "ok"
    assert captured["hooks"] is not None
