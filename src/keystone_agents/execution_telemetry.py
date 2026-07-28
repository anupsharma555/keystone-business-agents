"""Privacy-safe, monotonic execution timing contracts.

This module is intentionally independent from CLI, Slack, provider, and storage
adapters. Callers can adopt the contract additively without making telemetry a
new execution authority or requiring exported SDK tracing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from threading import RLock
from time import perf_counter_ns
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

EXECUTION_TELEMETRY_SCHEMA = "keystone.execution_telemetry.v1"
EXECUTION_STAGE_SPAN_SCHEMA = "keystone.execution_stage_span.v1"
EXECUTION_TELEMETRY_SUMMARY_SCHEMA = "keystone.execution_telemetry_summary.v1"

TelemetryStatus: TypeAlias = Literal["running", "completed", "failed"]
SpanStatus: TypeAlias = Literal["ok", "error", "cancelled"]
TelemetryScalar: TypeAlias = str | int | float | bool | None

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,95}$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|secret|token|password|credential|authorization|auth[_-]?token)",
    re.IGNORECASE,
)
_BODY_ATTRIBUTE_KEYS = frozenset(
    {
        "body",
        "content",
        "draft",
        "message",
        "prompt",
        "query",
        "request",
        "response",
        "text",
        "tool_input",
        "tool_output",
    }
)
_BODY_ATTRIBUTE_SUFFIXES = tuple(f"_{key}" for key in sorted(_BODY_ATTRIBUTE_KEYS))
_SAFE_DERIVED_ATTRIBUTE_SUFFIXES = ("_chars", "_count", "_hash", "_present")
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", re.IGNORECASE),
    re.compile(r"\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
)
_SAFE_ATTRIBUTE_KEYS = frozenset(
    {
        "agent",
        "agent_name",
        "cache_hit",
        "cache_status",
        "component",
        "event",
        "failure_kind",
        "live",
        "model",
        "model_name",
        "operation",
        "provider",
        "route",
        "run_mode",
        "source",
        "stage",
        "status",
        "tool_name",
    }
)
_SAFE_ATTRIBUTE_SUFFIXES = (
    "_attempt",
    "_chars",
    "_count",
    "_enabled",
    "_hash",
    "_kind",
    "_mode",
    "_ms",
    "_name",
    "_present",
    "_source",
    "_stage",
    "_status",
    "_version",
)
_MAX_ATTRIBUTE_STRING_CHARS = 160


class ExecutionTelemetryAttribute(BaseModel):
    """One bounded, scalar telemetry attribute."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: TelemetryScalar

    @field_validator("key", mode="before")
    @classmethod
    def _validate_key(cls, value: Any) -> str:
        key = str(value or "").strip().lower()
        if not _ATTRIBUTE_KEY_RE.fullmatch(key):
            raise ValueError("Telemetry attribute keys must be bounded snake_case labels.")
        if _sensitive_attribute_key(key):
            return key
        if key not in _SAFE_ATTRIBUTE_KEYS and not key.endswith(_SAFE_ATTRIBUTE_SUFFIXES):
            raise ValueError(f"Telemetry attribute key is not allowlisted: {key}")
        return key

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: Any, info: ValidationInfo) -> TelemetryScalar:
        if _sensitive_attribute_key(str(info.data.get("key") or "")):
            return "[REDACTED]"
        return _safe_scalar(value)


class ExecutionStageSpan(BaseModel):
    """One completed stage attempt measured from a shared monotonic origin."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_name: str = Field(default=EXECUTION_STAGE_SPAN_SCHEMA, alias="schema")
    span_id: str
    telemetry_id: str
    run_id: str
    parent_span_id: str = ""
    sequence: int = Field(ge=1)
    turn_index: int = Field(default=1, ge=1)
    attempt_index: int = Field(default=1, ge=1)
    stage: str
    status: SpanStatus
    started_offset_ms: float = Field(ge=0)
    finished_offset_ms: float = Field(ge=0)
    duration_ms: float = Field(ge=0)
    error_kind: str = ""
    attributes: tuple[ExecutionTelemetryAttribute, ...] = ()

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        if value != EXECUTION_STAGE_SPAN_SCHEMA:
            raise ValueError(f"Unsupported execution span schema: {value}")
        return value

    @field_validator("span_id", "telemetry_id", "run_id")
    @classmethod
    def _validate_required_identifier(cls, value: str) -> str:
        return _safe_identifier(value)

    @field_validator("parent_span_id")
    @classmethod
    def _validate_optional_identifier(cls, value: str) -> str:
        return _safe_identifier(value) if value else ""

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, value: str) -> str:
        return _safe_stage(value)

    @field_validator("error_kind")
    @classmethod
    def _validate_error_kind(cls, value: str) -> str:
        return _safe_label(value, max_chars=96)

    @field_validator("started_offset_ms", "finished_offset_ms", "duration_ms")
    @classmethod
    def _validate_finite_duration(cls, value: float) -> float:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("Telemetry timing values must be finite.")
        return round(numeric, 3)

    @model_validator(mode="after")
    def _validate_timing_and_failure(self) -> ExecutionStageSpan:
        if self.finished_offset_ms < self.started_offset_ms:
            raise ValueError("Execution span finish cannot precede its start.")
        expected = round(self.finished_offset_ms - self.started_offset_ms, 3)
        if not math.isclose(self.duration_ms, expected, abs_tol=0.001):
            raise ValueError("Execution span duration must match its monotonic offsets.")
        if self.status == "error" and not self.error_kind:
            raise ValueError("Failed execution spans require a safe error kind.")
        if self.status != "error" and self.error_kind:
            raise ValueError("Only failed execution spans may carry an error kind.")
        keys = [attribute.key for attribute in self.attributes]
        if len(keys) != len(set(keys)):
            raise ValueError("Execution span attributes must have unique keys.")
        return self

    @property
    def metadata(self) -> dict[str, TelemetryScalar]:
        """Return a copy of the bounded attribute mapping."""

        return {attribute.key: attribute.value for attribute in self.attributes}


class ExecutionTelemetrySummary(BaseModel):
    """Bounded aggregate metrics derived from immutable stage spans."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_name: str = Field(default=EXECUTION_TELEMETRY_SUMMARY_SCHEMA, alias="schema")
    status: TelemetryStatus
    span_count: int = Field(ge=0)
    failed_span_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    total_duration_ms: float = Field(ge=0)
    first_feedback_ms: float | None = Field(default=None, ge=0)
    final_response_ms: float | None = Field(default=None, ge=0)
    stage_duration_ms: dict[str, float] = Field(default_factory=dict)

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        if value != EXECUTION_TELEMETRY_SUMMARY_SCHEMA:
            raise ValueError(f"Unsupported execution telemetry summary schema: {value}")
        return value

    @field_validator("total_duration_ms", "first_feedback_ms", "final_response_ms")
    @classmethod
    def _validate_finite_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("Telemetry timing values must be finite.")
        return round(numeric, 3)

    @field_validator("stage_duration_ms")
    @classmethod
    def _validate_stage_durations(cls, value: dict[str, float]) -> dict[str, float]:
        return {
            _safe_stage(stage): _finite_nonnegative_ms(duration)
            for stage, duration in sorted(value.items())
        }

    @model_validator(mode="after")
    def _validate_feedback_order(self) -> ExecutionTelemetrySummary:
        if (
            self.first_feedback_ms is not None
            and self.final_response_ms is not None
            and self.first_feedback_ms > self.final_response_ms
        ):
            raise ValueError("First feedback cannot occur after the final response.")
        return self


class ExecutionTelemetry(BaseModel):
    """Immutable snapshot of one execution timeline."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_name: str = Field(default=EXECUTION_TELEMETRY_SCHEMA, alias="schema")
    telemetry_id: str
    run_id: str
    started_at_utc: datetime
    status: TelemetryStatus
    total_duration_ms: float = Field(ge=0)
    first_feedback_ms: float | None = Field(default=None, ge=0)
    final_response_ms: float | None = Field(default=None, ge=0)
    spans: tuple[ExecutionStageSpan, ...] = ()

    @field_validator("schema_name")
    @classmethod
    def _validate_schema(cls, value: str) -> str:
        if value != EXECUTION_TELEMETRY_SCHEMA:
            raise ValueError(f"Unsupported execution telemetry schema: {value}")
        return value

    @field_validator("telemetry_id", "run_id")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        return _safe_identifier(value)

    @field_validator("started_at_utc")
    @classmethod
    def _validate_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Execution telemetry timestamps must be timezone-aware.")
        return value.astimezone(UTC)

    @field_validator("total_duration_ms", "first_feedback_ms", "final_response_ms")
    @classmethod
    def _validate_finite_duration(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return _finite_nonnegative_ms(value)

    @model_validator(mode="after")
    def _validate_timeline(self) -> ExecutionTelemetry:
        seen_span_ids: set[str] = set()
        seen_attempts: set[tuple[int, int, str]] = set()
        prior_order: tuple[float, int, str] | None = None
        for span in self.spans:
            if span.telemetry_id != self.telemetry_id or span.run_id != self.run_id:
                raise ValueError("Execution spans must belong to the enclosing telemetry timeline.")
            if span.span_id in seen_span_ids:
                raise ValueError(f"Duplicate execution span id: {span.span_id}")
            attempt_key = (span.turn_index, span.attempt_index, span.stage)
            if attempt_key in seen_attempts:
                raise ValueError(
                    "Duplicate execution stage attempt: "
                    f"turn={span.turn_index}, attempt={span.attempt_index}, stage={span.stage}"
                )
            order = (span.started_offset_ms, span.sequence, span.span_id)
            if prior_order is not None and order < prior_order:
                raise ValueError("Execution spans must be ordered by monotonic start and sequence.")
            prior_order = order
            seen_span_ids.add(span.span_id)
            seen_attempts.add(attempt_key)
        known_ids = seen_span_ids
        for span in self.spans:
            if span.parent_span_id and span.parent_span_id not in known_ids:
                raise ValueError("Execution span parent must be present in the same timeline.")
        if (
            self.first_feedback_ms is not None
            and self.final_response_ms is not None
            and self.first_feedback_ms > self.final_response_ms
        ):
            raise ValueError("First feedback cannot occur after the final response.")
        latest_timing = max(
            [
                0.0,
                *(span.finished_offset_ms for span in self.spans),
                self.first_feedback_ms or 0.0,
                self.final_response_ms or 0.0,
            ]
        )
        if self.total_duration_ms < latest_timing:
            raise ValueError("Total duration cannot end before recorded telemetry activity.")
        if self.status == "completed" and self.final_response_ms is None:
            raise ValueError("Completed telemetry requires a final-response marker.")
        if self.status == "failed" and not any(span.status == "error" for span in self.spans):
            raise ValueError("Failed telemetry requires at least one failed stage span.")
        return self

    def summary(self) -> ExecutionTelemetrySummary:
        """Return aggregate timing without exposing span attributes."""

        stage_duration_ms: dict[str, float] = {}
        for span in self.spans:
            stage_duration_ms[span.stage] = round(
                stage_duration_ms.get(span.stage, 0.0) + span.duration_ms,
                3,
            )
        return ExecutionTelemetrySummary(
            status=self.status,
            span_count=len(self.spans),
            failed_span_count=sum(span.status == "error" for span in self.spans),
            turn_count=len({span.turn_index for span in self.spans}),
            attempt_count=len({(span.turn_index, span.attempt_index) for span in self.spans}),
            total_duration_ms=self.total_duration_ms,
            first_feedback_ms=self.first_feedback_ms,
            final_response_ms=self.final_response_ms,
            stage_duration_ms=stage_duration_ms,
        )

    def merge(self, *others: ExecutionTelemetry) -> ExecutionTelemetry:
        """Merge incremental snapshots from the same immutable timeline.

        Duplicate span IDs are idempotent only when their complete span payloads
        agree. A repeated turn/attempt/stage under a different span ID is rejected
        as ambiguous telemetry rather than being double-counted.
        """

        snapshots = (self, *others)
        for snapshot in snapshots[1:]:
            if (
                snapshot.telemetry_id != self.telemetry_id
                or snapshot.run_id != self.run_id
                or snapshot.started_at_utc != self.started_at_utc
            ):
                raise ValueError("Only snapshots from the same execution timeline can be merged.")

        spans_by_id: dict[str, ExecutionStageSpan] = {}
        attempts: dict[tuple[int, int, str], str] = {}
        for snapshot in snapshots:
            for span in snapshot.spans:
                existing = spans_by_id.get(span.span_id)
                if existing is not None:
                    if existing != span:
                        raise ValueError(f"Conflicting execution span payload: {span.span_id}")
                    continue
                attempt_key = (span.turn_index, span.attempt_index, span.stage)
                prior_span_id = attempts.get(attempt_key)
                if prior_span_id is not None and prior_span_id != span.span_id:
                    raise ValueError(
                        "Conflicting execution stage attempt: "
                        f"turn={span.turn_index}, attempt={span.attempt_index}, "
                        f"stage={span.stage}"
                    )
                attempts[attempt_key] = span.span_id
                spans_by_id[span.span_id] = span

        spans = tuple(
            sorted(
                spans_by_id.values(),
                key=lambda span: (span.started_offset_ms, span.sequence, span.span_id),
            )
        )
        first_feedback_values = [
            value
            for value in (snapshot.first_feedback_ms for snapshot in snapshots)
            if value is not None
        ]
        final_response_values = [
            value
            for value in (snapshot.final_response_ms for snapshot in snapshots)
            if value is not None
        ]
        statuses = {snapshot.status for snapshot in snapshots}
        status: TelemetryStatus = (
            "failed"
            if "failed" in statuses
            else "completed"
            if "completed" in statuses
            else "running"
        )
        return ExecutionTelemetry(
            telemetry_id=self.telemetry_id,
            run_id=self.run_id,
            started_at_utc=self.started_at_utc,
            status=status,
            total_duration_ms=max(snapshot.total_duration_ms for snapshot in snapshots),
            first_feedback_ms=min(first_feedback_values) if first_feedback_values else None,
            final_response_ms=max(final_response_values) if final_response_values else None,
            spans=spans,
        )


def compact_execution_telemetry(
    value: ExecutionTelemetry | ExecutionTelemetrySummary | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a storage-safe aggregate projection without IDs or span attributes."""

    if value is None:
        return {}
    try:
        if isinstance(value, ExecutionTelemetrySummary):
            summary = value
        elif isinstance(value, Mapping) and value.get("schema") == (
            EXECUTION_TELEMETRY_SUMMARY_SCHEMA
        ):
            summary = ExecutionTelemetrySummary.model_validate(value)
        else:
            telemetry = (
                value
                if isinstance(value, ExecutionTelemetry)
                else ExecutionTelemetry.model_validate(value)
            )
            summary = telemetry.summary()
    except (TypeError, ValueError):
        return {}
    return summary.model_dump(by_alias=True, mode="json")


class ExecutionSpanHandle:
    """Read-only identity exposed while a recorder-owned span is active."""

    __slots__ = ("_attempt_index", "_parent_span_id", "_span_id", "_stage", "_turn_index")

    def __init__(
        self,
        *,
        span_id: str,
        parent_span_id: str,
        stage: str,
        turn_index: int,
        attempt_index: int,
    ) -> None:
        object.__setattr__(self, "_span_id", span_id)
        object.__setattr__(self, "_parent_span_id", parent_span_id)
        object.__setattr__(self, "_stage", stage)
        object.__setattr__(self, "_turn_index", turn_index)
        object.__setattr__(self, "_attempt_index", attempt_index)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Execution span handles are immutable.")

    @property
    def span_id(self) -> str:
        return self._span_id

    @property
    def parent_span_id(self) -> str:
        return self._parent_span_id

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def turn_index(self) -> int:
        return self._turn_index

    @property
    def attempt_index(self) -> int:
        return self._attempt_index


class ExecutionTelemetryRecorder:
    """Mutable recorder that emits immutable telemetry snapshots."""

    def __init__(
        self,
        *,
        run_id: str | None = None,
        telemetry_id: str | None = None,
        clock_ns: Callable[[], int] = perf_counter_ns,
        started_at_utc: datetime | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock_ns = clock_ns
        self._id_factory = id_factory or _opaque_id
        self._run_id = _safe_identifier(run_id or f"run:{self._id_factory()}")
        self._telemetry_id = _safe_identifier(telemetry_id or f"telemetry:{self._id_factory()}")
        started = started_at_utc or datetime.now(UTC)
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("Execution telemetry timestamps must be timezone-aware.")
        self._started_at_utc = started.astimezone(UTC)
        self._origin_ns = _clock_value(clock_ns)
        self._lock = RLock()
        self._sequence = 0
        self._spans: list[ExecutionStageSpan] = []
        self._known_span_ids: set[str] = set()
        self._attempt_keys: set[tuple[int, int, str]] = set()
        self._first_feedback_ms: float | None = None
        self._final_response_ms: float | None = None
        self._active_span_ids: ContextVar[tuple[str, ...]] = ContextVar(
            f"execution_telemetry_active_spans_{self._telemetry_id}",
            default=(),
        )

    @property
    def telemetry_id(self) -> str:
        return self._telemetry_id

    @property
    def run_id(self) -> str:
        return self._run_id

    @contextmanager
    def span(
        self,
        stage: str,
        *,
        turn_index: int = 1,
        attempt_index: int = 1,
        attributes: Mapping[str, Any] | None = None,
        span_id: str | None = None,
    ) -> Iterator[ExecutionSpanHandle]:
        """Measure one stage attempt and re-raise failures unchanged."""

        safe_stage = _safe_stage(stage)
        if turn_index < 1 or attempt_index < 1:
            raise ValueError("Execution turn and attempt indexes must be positive.")
        safe_attributes = _safe_attributes(attributes)
        resolved_span_id = _safe_identifier(span_id or f"span:{self._id_factory()}")
        attempt_key = (turn_index, attempt_index, safe_stage)
        active = self._active_span_ids.get()
        parent_span_id = active[-1] if active else ""

        with self._lock:
            if resolved_span_id in self._known_span_ids:
                raise ValueError(f"Duplicate execution span id: {resolved_span_id}")
            if attempt_key in self._attempt_keys:
                raise ValueError(
                    "Duplicate execution stage attempt: "
                    f"turn={turn_index}, attempt={attempt_index}, stage={safe_stage}"
                )
            self._known_span_ids.add(resolved_span_id)
            self._attempt_keys.add(attempt_key)
            self._sequence += 1
            sequence = self._sequence

        started_ns = self._now_ns()
        token = self._active_span_ids.set((*active, resolved_span_id))
        handle = ExecutionSpanHandle(
            span_id=resolved_span_id,
            parent_span_id=parent_span_id,
            stage=safe_stage,
            turn_index=turn_index,
            attempt_index=attempt_index,
        )
        try:
            yield handle
        except BaseException as exc:
            self._finish_span(
                span_id=resolved_span_id,
                parent_span_id=parent_span_id,
                sequence=sequence,
                turn_index=turn_index,
                attempt_index=attempt_index,
                stage=safe_stage,
                status="error",
                error_kind=_exception_kind(exc),
                attributes=safe_attributes,
                started_ns=started_ns,
                token=token,
            )
            raise
        else:
            self._finish_span(
                span_id=resolved_span_id,
                parent_span_id=parent_span_id,
                sequence=sequence,
                turn_index=turn_index,
                attempt_index=attempt_index,
                stage=safe_stage,
                status="ok",
                error_kind="",
                attributes=safe_attributes,
                started_ns=started_ns,
                token=token,
            )

    def mark_first_feedback(self) -> float:
        """Record and return the earliest operator-visible feedback offset."""

        offset = self._offset_ms(self._now_ns())
        with self._lock:
            if self._first_feedback_ms is None:
                self._first_feedback_ms = offset
            return self._first_feedback_ms

    def mark_final_response(self) -> float:
        """Record the final result for the observed execution scope.

        This is operator-visible only when the recorder is owned by an entry
        adapter. An SDK-scoped recorder uses it as model-result-ready timing.
        """

        offset = self._offset_ms(self._now_ns())
        with self._lock:
            if self._first_feedback_ms is None:
                self._first_feedback_ms = offset
            if self._final_response_ms is None:
                self._final_response_ms = offset
            return self._final_response_ms

    def snapshot(self, *, status: TelemetryStatus | None = None) -> ExecutionTelemetry:
        """Return an immutable, deterministically ordered timeline snapshot."""

        if self._active_span_ids.get():
            raise RuntimeError("Cannot snapshot execution telemetry while a span is active.")
        now_ms = self._offset_ms(self._now_ns())
        with self._lock:
            spans = tuple(
                sorted(
                    self._spans,
                    key=lambda span: (span.started_offset_ms, span.sequence, span.span_id),
                )
            )
            failed = any(span.status == "error" for span in spans)
            resolved_status: TelemetryStatus = status or (
                "failed"
                if failed
                else "completed"
                if self._final_response_ms is not None
                else "running"
            )
            return ExecutionTelemetry(
                telemetry_id=self._telemetry_id,
                run_id=self._run_id,
                started_at_utc=self._started_at_utc,
                status=resolved_status,
                total_duration_ms=max(
                    now_ms,
                    *(span.finished_offset_ms for span in spans),
                    self._first_feedback_ms or 0.0,
                    self._final_response_ms or 0.0,
                ),
                first_feedback_ms=self._first_feedback_ms,
                final_response_ms=self._final_response_ms,
                spans=spans,
            )

    def _finish_span(
        self,
        *,
        span_id: str,
        parent_span_id: str,
        sequence: int,
        turn_index: int,
        attempt_index: int,
        stage: str,
        status: SpanStatus,
        error_kind: str,
        attributes: tuple[ExecutionTelemetryAttribute, ...],
        started_ns: int,
        token: Token[tuple[str, ...]],
    ) -> None:
        try:
            finished_ns = self._now_ns()
            started_offset_ms = self._offset_ms(started_ns)
            finished_offset_ms = self._offset_ms(finished_ns)
            span = ExecutionStageSpan(
                span_id=span_id,
                telemetry_id=self._telemetry_id,
                run_id=self._run_id,
                parent_span_id=parent_span_id,
                sequence=sequence,
                turn_index=turn_index,
                attempt_index=attempt_index,
                stage=stage,
                status=status,
                started_offset_ms=started_offset_ms,
                finished_offset_ms=finished_offset_ms,
                duration_ms=round(finished_offset_ms - started_offset_ms, 3),
                error_kind=error_kind,
                attributes=attributes,
            )
            with self._lock:
                self._spans.append(span)
        finally:
            self._active_span_ids.reset(token)

    def _now_ns(self) -> int:
        value = _clock_value(self._clock_ns)
        if value < self._origin_ns:
            raise ValueError("Monotonic telemetry clock moved before its origin.")
        return value

    def _offset_ms(self, value_ns: int) -> float:
        return round((value_ns - self._origin_ns) / 1_000_000, 3)


def _safe_attributes(
    attributes: Mapping[str, Any] | None,
) -> tuple[ExecutionTelemetryAttribute, ...]:
    if not attributes:
        return ()
    if len(attributes) > 24:
        raise ValueError("Execution telemetry spans accept at most 24 attributes.")
    return tuple(
        sorted(
            (
                ExecutionTelemetryAttribute(key=key, value=value)
                for key, value in attributes.items()
            ),
            key=lambda attribute: attribute.key,
        )
    )


def _safe_scalar(value: Any) -> TelemetryScalar:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Telemetry scalar numbers must be finite.")
        return value
    if not isinstance(value, str):
        raise ValueError("Telemetry attributes must be scalar values.")
    normalized = " ".join(value.strip().split())
    if len(normalized) > _MAX_ATTRIBUTE_STRING_CHARS:
        raise ValueError(
            f"Telemetry string attributes must be at most {_MAX_ATTRIBUTE_STRING_CHARS} characters."
        )
    for pattern in _SECRET_VALUE_PATTERNS:
        normalized = pattern.sub("[REDACTED]", normalized)
    return normalized


def _sensitive_attribute_key(key: str) -> bool:
    if key.endswith(_SAFE_DERIVED_ATTRIBUTE_SUFFIXES):
        return False
    return bool(
        _SECRET_KEY_RE.search(key)
        or key in _BODY_ATTRIBUTE_KEYS
        or key.endswith(_BODY_ATTRIBUTE_SUFFIXES)
    )


def _safe_identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError("Telemetry identifiers must be opaque bounded labels.")
    return identifier


def _safe_stage(value: Any) -> str:
    stage = str(value or "").strip().lower()
    if not _STAGE_RE.fullmatch(stage):
        raise ValueError("Telemetry stages must be bounded lowercase labels.")
    return stage


def _safe_label(value: Any, *, max_chars: int) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) > max_chars or (normalized and not _IDENTIFIER_RE.fullmatch(normalized)):
        raise ValueError("Telemetry labels must be opaque bounded values.")
    return normalized


def _exception_kind(exc: BaseException) -> str:
    """Return a safe class-only label without changing exception semantics."""

    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", type(exc).__name__).strip("_.:-")
    return (value or "Exception")[:96]


def _finite_nonnegative_ms(value: Any) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError("Telemetry timing values must be finite and nonnegative.")
    return round(numeric, 3)


def _clock_value(clock_ns: Callable[[], int]) -> int:
    value = clock_ns()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Telemetry clocks must return nonnegative integer nanoseconds.")
    return value


def _opaque_id() -> str:
    return uuid4().hex
