from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from keystone_agents.runtime.execution_deadline import (
    EXECUTION_DEADLINE_CORRELATION_ENV,
    EXECUTION_FINALIZATION_RESERVE_MS_ENV,
    EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV,
    EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV,
    ExecutionDeadlineConfigurationError,
    ExecutionDeadlineExceeded,
    ExecutionDeadlineLedger,
    activate_execution_deadline,
    current_execution_deadline,
    current_execution_deadline_snapshot,
)


class _FakeClock:
    def __init__(self) -> None:
        self.wall_ms = 1_800_000_000_000
        self.monotonic_ms = 50_000

    def wall(self) -> int:
        return self.wall_ms

    def monotonic(self) -> int:
        return self.monotonic_ms

    def advance(self, milliseconds: int) -> None:
        self.wall_ms += milliseconds
        self.monotonic_ms += milliseconds


def _deadline(
    clock: _FakeClock,
    *,
    soft_seconds: float = 10,
    hard_seconds: float = 15,
    reserve_seconds: float = 2,
) -> ExecutionDeadlineLedger:
    return ExecutionDeadlineLedger.from_timeouts(
        soft_timeout_seconds=soft_seconds,
        hard_timeout_seconds=hard_seconds,
        finalization_reserve_seconds=reserve_seconds,
        correlation_id="deadline-test",
        wall_clock_ms=clock.wall,
        monotonic_clock_ms=clock.monotonic,
    )


def test_soft_deadline_stops_new_work_but_preserves_hard_finalization_window() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)

    deadline.admit(stage="chief_of_staff:llm_start", boundary="llm")
    clock.advance(10_000)
    deadline.checkpoint(stage="chief_of_staff:llm_end", boundary="llm")

    assert deadline.remaining_soft_ms == 0
    assert deadline.remaining_hard_ms == 5_000
    assert deadline.finalization_active is True
    with pytest.raises(ExecutionDeadlineExceeded) as raised:
        deadline.admit(stage="chief_of_staff:repair", boundary="llm")

    assert raised.value.telemetry()["rejected_before_dispatch"] is True
    snapshot = deadline.snapshot()
    assert [event["phase"] for event in snapshot["events"]] == [
        "admitted",
        "completed",
        "rejected",
    ]
    assert snapshot["in_flight_preemption_supported"] is False


def test_absolute_deadline_envelope_is_frozen_and_round_trips_through_environment() -> None:
    clock = _FakeClock()
    parent = _deadline(clock, soft_seconds=100, hard_seconds=140, reserve_seconds=10)

    inherited = ExecutionDeadlineLedger.from_environment(
        parent.environment(),
        wall_clock_ms=clock.wall,
        monotonic_clock_ms=clock.monotonic,
    )

    assert inherited is not None
    assert inherited.soft_deadline_epoch_ms == parent.soft_deadline_epoch_ms
    assert inherited.hard_deadline_epoch_ms == parent.hard_deadline_epoch_ms
    assert inherited.finalization_reserve_ms == parent.finalization_reserve_ms
    assert inherited.correlation_id == parent.correlation_id
    with pytest.raises(FrozenInstanceError):
        inherited.hard_deadline_epoch_ms += 1


def test_child_may_shorten_but_cannot_extend_inherited_deadlines() -> None:
    clock = _FakeClock()
    parent = _deadline(clock, soft_seconds=100, hard_seconds=140, reserve_seconds=10)

    attempted_extension = ExecutionDeadlineLedger.from_environment(
        parent.environment(),
        soft_timeout_seconds=200,
        hard_timeout_seconds=300,
        wall_clock_ms=clock.wall,
        monotonic_clock_ms=clock.monotonic,
    )
    shortened = ExecutionDeadlineLedger.from_environment(
        parent.environment(),
        soft_timeout_seconds=20,
        hard_timeout_seconds=35,
        wall_clock_ms=clock.wall,
        monotonic_clock_ms=clock.monotonic,
    )

    assert attempted_extension is not None
    assert attempted_extension.soft_deadline_epoch_ms == parent.soft_deadline_epoch_ms
    assert attempted_extension.hard_deadline_epoch_ms == parent.hard_deadline_epoch_ms
    assert shortened is not None
    assert shortened.soft_deadline_epoch_ms == clock.wall_ms + 20_000
    assert shortened.hard_deadline_epoch_ms == clock.wall_ms + 35_000
    assert (
        shortened.hard_deadline_epoch_ms - shortened.soft_deadline_epoch_ms
        >= parent.finalization_reserve_ms
    )


def test_short_child_timeout_may_reduce_reserve_without_extending_parent() -> None:
    clock = _FakeClock()
    parent = _deadline(clock, soft_seconds=100, hard_seconds=140, reserve_seconds=10)

    shortened = parent.shortened(
        soft_timeout_seconds=1.5,
        hard_timeout_seconds=2,
        finalization_reserve_seconds=0.5,
    )

    assert shortened.soft_deadline_epoch_ms == clock.wall_ms + 1_500
    assert shortened.hard_deadline_epoch_ms == clock.wall_ms + 2_000
    assert shortened.finalization_reserve_ms == 500
    assert shortened.correlation_id == parent.correlation_id


def test_retry_wait_is_rejected_when_it_would_consume_soft_headroom() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock, soft_seconds=5, hard_seconds=12, reserve_seconds=2)

    assert deadline.admit_wait(
        stage="gmail_triage:retry_wait",
        boundary="retry_wait",
        wait_seconds=2,
    ) == 2.0
    with pytest.raises(ExecutionDeadlineExceeded) as raised:
        deadline.admit_wait(
            stage="gmail_triage:retry_wait",
            boundary="retry_wait",
            wait_seconds=5,
        )

    assert raised.value.reason == "insufficient_headroom"
    assert raised.value.required_headroom_ms == 5_000
    assert deadline.snapshot()["events"][-1]["phase"] == (
        "rejected_insufficient_headroom"
    )


def test_inherited_environment_requires_one_complete_coherent_envelope() -> None:
    clock = _FakeClock()
    partial = {
        EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV: str(clock.wall_ms + 10_000),
        EXECUTION_DEADLINE_CORRELATION_ENV: "partial",
    }
    incoherent = {
        EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV: str(clock.wall_ms + 20_000),
        EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV: str(clock.wall_ms + 10_000),
        EXECUTION_FINALIZATION_RESERVE_MS_ENV: "1000",
        EXECUTION_DEADLINE_CORRELATION_ENV: "incoherent",
    }

    with pytest.raises(ExecutionDeadlineConfigurationError, match="incomplete"):
        ExecutionDeadlineLedger.from_environment(
            partial,
            wall_clock_ms=clock.wall,
            monotonic_clock_ms=clock.monotonic,
        )
    with pytest.raises(ExecutionDeadlineConfigurationError, match="cannot precede"):
        ExecutionDeadlineLedger.from_environment(
            incoherent,
            wall_clock_ms=clock.wall,
            monotonic_clock_ms=clock.monotonic,
        )


@pytest.mark.parametrize("soft_seconds", [45, 120, 300])
def test_existing_quality_work_budgets_can_be_expressed_without_changing_them(
    soft_seconds: int,
) -> None:
    clock = _FakeClock()
    deadline = _deadline(
        clock,
        soft_seconds=soft_seconds,
        hard_seconds=soft_seconds + 50,
        reserve_seconds=5,
    )

    assert deadline.remaining_soft_ms == soft_seconds * 1000


def test_request_context_is_scoped_and_snapshot_contains_no_boundary_content() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)

    assert current_execution_deadline(load_environment=False) is None
    with activate_execution_deadline(deadline):
        assert current_execution_deadline(load_environment=False) is deadline
        deadline.admit(stage="Gmail Triage tool query_gmail", boundary="tool")
        snapshot = current_execution_deadline_snapshot()
    assert current_execution_deadline(load_environment=False) is None

    assert snapshot is not None
    event = snapshot["events"][0]
    assert event["stage"] == "Gmail_Triage_tool_query_gmail"
    assert set(event) == {
        "ordinal",
        "stage",
        "boundary",
        "phase",
        "remaining_soft_ms",
        "remaining_hard_ms",
    }


def test_environment_contract_uses_absolute_milliseconds() -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)
    environment = deadline.environment()

    assert environment == {
        EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV: str(clock.wall_ms + 10_000),
        EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV: str(clock.wall_ms + 15_000),
        EXECUTION_FINALIZATION_RESERVE_MS_ENV: "2000",
        EXECUTION_DEADLINE_CORRELATION_ENV: "deadline-test",
    }


def test_lazy_inherited_deadline_clears_when_environment_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeClock()
    deadline = _deadline(clock)
    for key, value in deadline.environment().items():
        monkeypatch.setenv(key, value)

    inherited = current_execution_deadline()
    assert inherited is not None
    assert inherited.correlation_id == "deadline-test"

    for key in deadline.environment():
        monkeypatch.delenv(key, raising=False)

    assert current_execution_deadline(load_environment=False) is None
