"""Request-local soft and hard deadlines for bounded agent execution.

The soft deadline is an admission boundary: no new model or tool call should
start after it. The hard deadline is a parent-process safety boundary that may
include one already-admitted in-flight call and final result serialization.
Neither boundary claims to interrupt provider work that is already in flight.
"""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from uuid import uuid4

EXECUTION_DEADLINE_SCHEMA = "keystone.execution_deadline.v1"
EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV = "KEYSTONE_EXECUTION_SOFT_DEADLINE_EPOCH_MS"
EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV = "KEYSTONE_EXECUTION_HARD_DEADLINE_EPOCH_MS"
EXECUTION_FINALIZATION_RESERVE_MS_ENV = "KEYSTONE_EXECUTION_FINALIZATION_RESERVE_MS"
EXECUTION_DEADLINE_CORRELATION_ENV = "KEYSTONE_EXECUTION_DEADLINE_CORRELATION_ID"

_DEADLINE_ENV_KEYS = (
    EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV,
    EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV,
    EXECUTION_FINALIZATION_RESERVE_MS_ENV,
    EXECUTION_DEADLINE_CORRELATION_ENV,
)
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.:/-]+")


def _system_wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _system_monotonic_clock_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _safe_label(value: object, *, fallback: str) -> str:
    normalized = _SAFE_LABEL_RE.sub("_", str(value or "").strip()).strip("_")
    return (normalized or fallback)[:160]


def _milliseconds_from_seconds(value: object, *, field_name: str) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number.") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number.")
    return int(round(seconds * 1000.0))


class ExecutionDeadlineConfigurationError(ValueError):
    """An inherited deadline environment is partial, malformed, or incoherent."""


class ExecutionDeadlineExceeded(RuntimeError):
    """A model or tool boundary was rejected before provider dispatch."""

    def __init__(
        self,
        *,
        stage: str,
        boundary: str,
        correlation_id: str,
        remaining_soft_ms: int,
        remaining_hard_ms: int,
        reason: str = "soft_deadline_elapsed",
        required_headroom_ms: int = 0,
    ) -> None:
        self.stage = _safe_label(stage, fallback="execution_boundary")
        self.boundary = _safe_label(boundary, fallback="boundary")
        self.correlation_id = _safe_label(correlation_id, fallback="deadline")
        self.remaining_soft_ms = max(0, int(remaining_soft_ms))
        self.remaining_hard_ms = max(0, int(remaining_hard_ms))
        self.reason = _safe_label(reason, fallback="soft_deadline_elapsed")
        self.required_headroom_ms = max(0, int(required_headroom_ms))
        if self.reason == "insufficient_headroom":
            message = (
                "Execution deadline lacks enough headroom before "
                f"{self.stage} ({self.boundary}); no wait or provider call was started."
            )
        else:
            message = (
                "Execution soft deadline elapsed before "
                f"{self.stage} ({self.boundary}); no provider call was started."
            )
        super().__init__(message)

    def telemetry(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_DEADLINE_SCHEMA,
            "correlation_id": self.correlation_id,
            "stage": self.stage,
            "boundary": self.boundary,
            "remaining_soft_ms": self.remaining_soft_ms,
            "remaining_hard_ms": self.remaining_hard_ms,
            "reason": self.reason,
            "required_headroom_ms": self.required_headroom_ms,
            "rejected_before_dispatch": True,
            "in_flight_preemption_supported": False,
        }


@dataclass(frozen=True)
class ExecutionDeadlineLedger:
    """One immutable deadline envelope with a mutable trace-safe event journal."""

    soft_deadline_epoch_ms: int
    hard_deadline_epoch_ms: int
    finalization_reserve_ms: int
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    _wall_clock_ms: Callable[[], int] = field(
        default=_system_wall_clock_ms,
        repr=False,
        compare=False,
    )
    _monotonic_clock_ms: Callable[[], int] = field(
        default=_system_monotonic_clock_ms,
        repr=False,
        compare=False,
    )
    _events: list[dict[str, Any]] = field(
        default_factory=list,
        init=False,
        repr=False,
        compare=False,
    )
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)
    _soft_deadline_monotonic_ms: int = field(init=False, repr=False, compare=False)
    _hard_deadline_monotonic_ms: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        soft = int(self.soft_deadline_epoch_ms)
        hard = int(self.hard_deadline_epoch_ms)
        reserve = int(self.finalization_reserve_ms)
        if soft < 0 or hard < 0 or reserve < 0:
            raise ExecutionDeadlineConfigurationError(
                "Execution deadline values must be non-negative milliseconds."
            )
        if hard < soft:
            raise ExecutionDeadlineConfigurationError(
                "The execution hard deadline cannot precede the soft deadline."
            )
        if hard - soft < reserve:
            raise ExecutionDeadlineConfigurationError(
                "The hard deadline must preserve the declared finalization reserve."
            )
        wall_now = int(self._wall_clock_ms())
        monotonic_now = int(self._monotonic_clock_ms())
        object.__setattr__(self, "soft_deadline_epoch_ms", soft)
        object.__setattr__(self, "hard_deadline_epoch_ms", hard)
        object.__setattr__(self, "finalization_reserve_ms", reserve)
        object.__setattr__(
            self,
            "correlation_id",
            _safe_label(self.correlation_id, fallback=uuid4().hex),
        )
        object.__setattr__(
            self,
            "_soft_deadline_monotonic_ms",
            monotonic_now + (soft - wall_now),
        )
        object.__setattr__(
            self,
            "_hard_deadline_monotonic_ms",
            monotonic_now + (hard - wall_now),
        )

    @classmethod
    def from_timeouts(
        cls,
        *,
        soft_timeout_seconds: float,
        hard_timeout_seconds: float | None = None,
        finalization_reserve_seconds: float = 0.0,
        correlation_id: str | None = None,
        wall_clock_ms: Callable[[], int] = _system_wall_clock_ms,
        monotonic_clock_ms: Callable[[], int] = _system_monotonic_clock_ms,
    ) -> ExecutionDeadlineLedger:
        """Create one local deadline envelope from bounded relative timeouts."""

        soft_timeout_ms = _milliseconds_from_seconds(
            soft_timeout_seconds,
            field_name="soft_timeout_seconds",
        )
        reserve_ms = _milliseconds_from_seconds(
            finalization_reserve_seconds,
            field_name="finalization_reserve_seconds",
        )
        hard_timeout_ms = (
            soft_timeout_ms + reserve_ms
            if hard_timeout_seconds is None
            else _milliseconds_from_seconds(
                hard_timeout_seconds,
                field_name="hard_timeout_seconds",
            )
        )
        wall_now = int(wall_clock_ms())
        return cls(
            soft_deadline_epoch_ms=wall_now + soft_timeout_ms,
            hard_deadline_epoch_ms=wall_now + hard_timeout_ms,
            finalization_reserve_ms=reserve_ms,
            correlation_id=correlation_id or uuid4().hex,
            _wall_clock_ms=wall_clock_ms,
            _monotonic_clock_ms=monotonic_clock_ms,
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        soft_timeout_seconds: float | None = None,
        hard_timeout_seconds: float | None = None,
        wall_clock_ms: Callable[[], int] = _system_wall_clock_ms,
        monotonic_clock_ms: Callable[[], int] = _system_monotonic_clock_ms,
    ) -> ExecutionDeadlineLedger | None:
        """Load an inherited envelope and optionally shorten its absolute caps."""

        values = os.environ if environ is None else environ
        present = {key for key in _DEADLINE_ENV_KEYS if str(values.get(key, "")).strip()}
        if not present:
            return None
        if present != set(_DEADLINE_ENV_KEYS):
            missing = sorted(set(_DEADLINE_ENV_KEYS) - present)
            raise ExecutionDeadlineConfigurationError(
                "Inherited execution deadline environment is incomplete: "
                + ", ".join(missing)
            )
        try:
            inherited = cls(
                soft_deadline_epoch_ms=int(values[EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV]),
                hard_deadline_epoch_ms=int(values[EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV]),
                finalization_reserve_ms=int(values[EXECUTION_FINALIZATION_RESERVE_MS_ENV]),
                correlation_id=values[EXECUTION_DEADLINE_CORRELATION_ENV],
                _wall_clock_ms=wall_clock_ms,
                _monotonic_clock_ms=monotonic_clock_ms,
            )
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ExecutionDeadlineConfigurationError):
                raise
            raise ExecutionDeadlineConfigurationError(
                "Inherited execution deadline environment contains invalid milliseconds."
            ) from exc
        if soft_timeout_seconds is None and hard_timeout_seconds is None:
            return inherited
        return inherited.shortened(
            soft_timeout_seconds=soft_timeout_seconds,
            hard_timeout_seconds=hard_timeout_seconds,
        )

    @property
    def remaining_soft_ms(self) -> int:
        return max(0, self._soft_deadline_monotonic_ms - int(self._monotonic_clock_ms()))

    @property
    def remaining_hard_ms(self) -> int:
        return max(0, self._hard_deadline_monotonic_ms - int(self._monotonic_clock_ms()))

    @property
    def soft_deadline_exceeded(self) -> bool:
        return self.remaining_soft_ms <= 0

    @property
    def hard_deadline_exceeded(self) -> bool:
        return self.remaining_hard_ms <= 0

    @property
    def finalization_active(self) -> bool:
        return self.soft_deadline_exceeded and not self.hard_deadline_exceeded

    def shortened(
        self,
        *,
        soft_timeout_seconds: float | None = None,
        hard_timeout_seconds: float | None = None,
        finalization_reserve_seconds: float | None = None,
    ) -> ExecutionDeadlineLedger:
        """Return a child envelope whose absolute caps never exceed this envelope."""

        wall_now = int(self._wall_clock_ms())
        reserve_ms = self.finalization_reserve_ms
        if finalization_reserve_seconds is not None:
            reserve_ms = min(
                reserve_ms,
                _milliseconds_from_seconds(
                    finalization_reserve_seconds,
                    field_name="finalization_reserve_seconds",
                ),
            )
        requested_soft = self.soft_deadline_epoch_ms
        if soft_timeout_seconds is not None:
            requested_soft = wall_now + _milliseconds_from_seconds(
                soft_timeout_seconds,
                field_name="soft_timeout_seconds",
            )
        requested_hard = self.hard_deadline_epoch_ms
        if hard_timeout_seconds is not None:
            requested_hard = wall_now + _milliseconds_from_seconds(
                hard_timeout_seconds,
                field_name="hard_timeout_seconds",
            )
        hard = min(self.hard_deadline_epoch_ms, requested_hard)
        soft = min(
            self.soft_deadline_epoch_ms,
            requested_soft,
            hard - reserve_ms,
        )
        return ExecutionDeadlineLedger(
            soft_deadline_epoch_ms=max(0, soft),
            hard_deadline_epoch_ms=hard,
            finalization_reserve_ms=reserve_ms,
            correlation_id=self.correlation_id,
            _wall_clock_ms=self._wall_clock_ms,
            _monotonic_clock_ms=self._monotonic_clock_ms,
        )

    def environment(self) -> dict[str, str]:
        """Return the immutable absolute envelope for one sequential child process."""

        return {
            EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV: str(self.soft_deadline_epoch_ms),
            EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV: str(self.hard_deadline_epoch_ms),
            EXECUTION_FINALIZATION_RESERVE_MS_ENV: str(self.finalization_reserve_ms),
            EXECUTION_DEADLINE_CORRELATION_ENV: self.correlation_id,
        }

    def admit(self, *, stage: str, boundary: str) -> None:
        """Admit one new boundary or fail before its provider dispatch."""

        remaining_soft_ms = self.remaining_soft_ms
        remaining_hard_ms = self.remaining_hard_ms
        if remaining_soft_ms <= 0:
            self._append_event(
                stage=stage,
                boundary=boundary,
                phase="rejected",
                remaining_soft_ms=remaining_soft_ms,
                remaining_hard_ms=remaining_hard_ms,
            )
            raise ExecutionDeadlineExceeded(
                stage=stage,
                boundary=boundary,
                correlation_id=self.correlation_id,
                remaining_soft_ms=remaining_soft_ms,
                remaining_hard_ms=remaining_hard_ms,
            )
        self._append_event(
            stage=stage,
            boundary=boundary,
            phase="admitted",
            remaining_soft_ms=remaining_soft_ms,
            remaining_hard_ms=remaining_hard_ms,
        )

    def admit_wait(
        self,
        *,
        stage: str,
        boundary: str,
        wait_seconds: float,
    ) -> float:
        """Admit one bounded wait only when a later boundary can still start."""

        wait_ms = _milliseconds_from_seconds(wait_seconds, field_name="wait_seconds")
        remaining_soft_ms = self.remaining_soft_ms
        remaining_hard_ms = self.remaining_hard_ms
        hard_work_ms = max(0, remaining_hard_ms - self.finalization_reserve_ms)
        if remaining_soft_ms <= wait_ms or hard_work_ms <= wait_ms:
            self._append_event(
                stage=stage,
                boundary=boundary,
                phase="rejected_insufficient_headroom",
                remaining_soft_ms=remaining_soft_ms,
                remaining_hard_ms=remaining_hard_ms,
            )
            raise ExecutionDeadlineExceeded(
                stage=stage,
                boundary=boundary,
                correlation_id=self.correlation_id,
                remaining_soft_ms=remaining_soft_ms,
                remaining_hard_ms=remaining_hard_ms,
                reason="insufficient_headroom",
                required_headroom_ms=wait_ms,
            )
        self._append_event(
            stage=stage,
            boundary=boundary,
            phase="admitted",
            remaining_soft_ms=remaining_soft_ms,
            remaining_hard_ms=remaining_hard_ms,
        )
        return wait_ms / 1000.0

    def checkpoint(self, *, stage: str, boundary: str, phase: str = "completed") -> None:
        """Record safe boundary progress without inspecting model or tool content."""

        self._append_event(
            stage=stage,
            boundary=boundary,
            phase=phase,
            remaining_soft_ms=self.remaining_soft_ms,
            remaining_hard_ms=self.remaining_hard_ms,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a trace-safe immutable view of deadline state and boundary progress."""

        remaining_soft_ms = self.remaining_soft_ms
        remaining_hard_ms = self.remaining_hard_ms
        with self._lock:
            events = [dict(event) for event in self._events]
        return {
            "schema": EXECUTION_DEADLINE_SCHEMA,
            "correlation_id": self.correlation_id,
            "soft_deadline_epoch_ms": self.soft_deadline_epoch_ms,
            "hard_deadline_epoch_ms": self.hard_deadline_epoch_ms,
            "finalization_reserve_ms": self.finalization_reserve_ms,
            "remaining_soft_ms": remaining_soft_ms,
            "remaining_hard_ms": remaining_hard_ms,
            "soft_deadline_exceeded": remaining_soft_ms <= 0,
            "hard_deadline_exceeded": remaining_hard_ms <= 0,
            "finalization_active": remaining_soft_ms <= 0 < remaining_hard_ms,
            "enforcement": "pre_boundary_admission",
            "in_flight_preemption_supported": False,
            "events": events,
        }

    def _append_event(
        self,
        *,
        stage: str,
        boundary: str,
        phase: str,
        remaining_soft_ms: int,
        remaining_hard_ms: int,
    ) -> None:
        safe_stage = _safe_label(stage, fallback="execution_boundary")
        safe_boundary = _safe_label(boundary, fallback="boundary")
        safe_phase = _safe_label(phase, fallback="checkpoint")
        with self._lock:
            self._events.append(
                {
                    "ordinal": len(self._events) + 1,
                    "stage": safe_stage,
                    "boundary": safe_boundary,
                    "phase": safe_phase,
                    "remaining_soft_ms": max(0, int(remaining_soft_ms)),
                    "remaining_hard_ms": max(0, int(remaining_hard_ms)),
                }
            )


_ACTIVE_EXECUTION_DEADLINE: ContextVar[ExecutionDeadlineLedger | None] = ContextVar(
    "keystone_active_execution_deadline",
    default=None,
)
_INHERITED_EXECUTION_DEADLINE_SIGNATURE: ContextVar[tuple[str, ...] | None] = ContextVar(
    "keystone_inherited_execution_deadline_signature",
    default=None,
)


def _deadline_environment_signature(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...] | None:
    values = os.environ if environ is None else environ
    signature = tuple(str(values.get(key, "") or "").strip() for key in _DEADLINE_ENV_KEYS)
    return signature if any(signature) else None


@contextmanager
def activate_execution_deadline(
    deadline: ExecutionDeadlineLedger,
) -> Iterator[ExecutionDeadlineLedger]:
    """Activate one deadline envelope for the current request context."""

    token = _ACTIVE_EXECUTION_DEADLINE.set(deadline)
    inherited_token = _INHERITED_EXECUTION_DEADLINE_SIGNATURE.set(None)
    try:
        yield deadline
    finally:
        _INHERITED_EXECUTION_DEADLINE_SIGNATURE.reset(inherited_token)
        _ACTIVE_EXECUTION_DEADLINE.reset(token)


def current_execution_deadline(
    *,
    load_environment: bool = True,
) -> ExecutionDeadlineLedger | None:
    """Return the active ledger, lazily inheriting an absolute child envelope."""

    deadline = _ACTIVE_EXECUTION_DEADLINE.get()
    inherited_signature = _INHERITED_EXECUTION_DEADLINE_SIGNATURE.get()
    if deadline is not None and inherited_signature is not None:
        if inherited_signature != _deadline_environment_signature():
            _ACTIVE_EXECUTION_DEADLINE.set(None)
            _INHERITED_EXECUTION_DEADLINE_SIGNATURE.set(None)
            deadline = None
        else:
            return deadline
    if deadline is not None or not load_environment:
        return deadline
    inherited = ExecutionDeadlineLedger.from_environment()
    if inherited is not None:
        _ACTIVE_EXECUTION_DEADLINE.set(inherited)
        _INHERITED_EXECUTION_DEADLINE_SIGNATURE.set(_deadline_environment_signature())
    return inherited


def current_execution_deadline_snapshot() -> dict[str, Any] | None:
    deadline = current_execution_deadline(load_environment=False)
    return deadline.snapshot() if deadline is not None else None


__all__ = [
    "EXECUTION_DEADLINE_CORRELATION_ENV",
    "EXECUTION_DEADLINE_SCHEMA",
    "EXECUTION_FINALIZATION_RESERVE_MS_ENV",
    "EXECUTION_HARD_DEADLINE_EPOCH_MS_ENV",
    "EXECUTION_SOFT_DEADLINE_EPOCH_MS_ENV",
    "ExecutionDeadlineConfigurationError",
    "ExecutionDeadlineExceeded",
    "ExecutionDeadlineLedger",
    "activate_execution_deadline",
    "current_execution_deadline",
    "current_execution_deadline_snapshot",
]
