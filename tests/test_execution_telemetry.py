from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from keystone_agents.execution_telemetry import (
    EXECUTION_STAGE_SPAN_SCHEMA,
    EXECUTION_TELEMETRY_SCHEMA,
    EXECUTION_TELEMETRY_SUMMARY_SCHEMA,
    ExecutionTelemetry,
    ExecutionTelemetryRecorder,
    compact_execution_telemetry,
)


class _Clock:
    def __init__(self, *milliseconds: int) -> None:
        self._values = iter(value * 1_000_000 for value in milliseconds)

    def __call__(self) -> int:
        return next(self._values)


def _ids(*values: str):
    return iter(values).__next__


def _started_at() -> datetime:
    return datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def test_recorder_orders_nested_spans_by_start_and_preserves_parent() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:nesting",
        telemetry_id="telemetry:nesting",
        clock_ns=_Clock(0, 1, 2, 5, 8, 9, 12, 13),
        started_at_utc=_started_at(),
        id_factory=_ids("outer", "inner"),
    )

    with recorder.span("orchestrator.preflight") as outer:
        with recorder.span("orchestrator.planner") as inner:
            assert inner.parent_span_id == outer.span_id
            assert inner.turn_index == 1
            assert inner.attempt_index == 1

    assert recorder.mark_first_feedback() == 9.0
    assert recorder.mark_final_response() == 12.0
    telemetry = recorder.snapshot()

    assert telemetry.schema_name == EXECUTION_TELEMETRY_SCHEMA
    assert [span.stage for span in telemetry.spans] == [
        "orchestrator.preflight",
        "orchestrator.planner",
    ]
    assert telemetry.spans[0].parent_span_id == ""
    assert telemetry.spans[1].parent_span_id == telemetry.spans[0].span_id
    assert telemetry.spans[0].duration_ms == 7.0
    assert telemetry.spans[1].duration_ms == 3.0
    assert telemetry.total_duration_ms == 13.0

    summary = telemetry.summary()
    assert summary.schema_name == EXECUTION_TELEMETRY_SUMMARY_SCHEMA
    assert summary.span_count == 2
    assert summary.turn_count == 1
    assert summary.attempt_count == 1
    assert summary.first_feedback_ms == 9.0
    assert summary.final_response_ms == 12.0
    assert summary.stage_duration_ms == {
        "orchestrator.planner": 3.0,
        "orchestrator.preflight": 7.0,
    }

    compact = compact_execution_telemetry(telemetry)
    assert compact["schema"] == EXECUTION_TELEMETRY_SUMMARY_SCHEMA
    assert compact["final_response_ms"] == 12.0
    assert compact["stage_duration_ms"] == summary.stage_duration_ms
    assert "telemetry_id" not in compact
    assert "run_id" not in compact
    assert "spans" not in compact


def test_compact_execution_telemetry_fails_closed_for_invalid_payload() -> None:
    assert compact_execution_telemetry({"request": "private body"}) == {}


def test_duplicate_stage_attempt_is_rejected_but_next_attempt_is_retained() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:attempts",
        telemetry_id="telemetry:attempts",
        clock_ns=_Clock(0, 1, 2, 3, 5, 6),
        started_at_utc=_started_at(),
        id_factory=_ids("attempt-one", "attempt-two", "duplicate"),
    )

    with recorder.span("sdk.model", turn_index=2, attempt_index=1):
        pass

    with pytest.raises(ValueError, match="Duplicate execution stage attempt"):
        with recorder.span("sdk.model", turn_index=2, attempt_index=1):
            pass

    with recorder.span("sdk.model", turn_index=2, attempt_index=2):
        pass

    telemetry = recorder.snapshot()
    assert [(span.turn_index, span.attempt_index) for span in telemetry.spans] == [
        (2, 1),
        (2, 2),
    ]
    assert telemetry.summary().attempt_count == 2


def test_span_attributes_are_scalar_bounded_and_redacted() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:redaction",
        telemetry_id="telemetry:redaction",
        clock_ns=_Clock(0, 1, 2, 3),
        started_at_utc=_started_at(),
        id_factory=_ids("redacted-span"),
    )

    with recorder.span(
        "sdk.model",
        attributes={
            "api_key": "sk-secretvalue123",
            "prompt": "raw customer prompt",
            "prompt_cache_key_hash": "abc123def456",
            "provider": "Bearer-like safe label with sk-secretvalue123",
            "retry_count": 2,
        },
    ):
        pass

    span = recorder.snapshot().spans[0]
    assert span.schema_name == EXECUTION_STAGE_SPAN_SCHEMA
    assert span.metadata == {
        "api_key": "[REDACTED]",
        "prompt": "[REDACTED]",
        "prompt_cache_key_hash": "abc123def456",
        "provider": "Bearer-like safe label with [REDACTED]",
        "retry_count": 2,
    }
    serialized = span.model_dump_json(by_alias=True)
    assert "sk-secretvalue123" not in serialized
    assert "raw customer prompt" not in serialized


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({"arbitrary": "value"}, "not allowlisted"),
        ({"provider": {"nested": "value"}}, "scalar values"),
        ({"provider": float("inf")}, "finite"),
        ({"provider": "x" * 161}, "at most 160"),
    ],
)
def test_span_attributes_reject_unsafe_values(
    attributes: dict[str, Any],
    message: str,
) -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:unsafe",
        telemetry_id="telemetry:unsafe",
        clock_ns=_Clock(0),
        started_at_utc=_started_at(),
    )

    with pytest.raises(ValueError, match=message):
        with recorder.span("sdk.model", attributes=attributes):
            pass


def test_failed_span_records_only_exception_kind_and_reraises() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:failure",
        telemetry_id="telemetry:failure",
        clock_ns=_Clock(0, 1, 4, 5),
        started_at_utc=_started_at(),
        id_factory=_ids("failed-span"),
    )

    with pytest.raises(RuntimeError, match="private failure details"):
        with recorder.span("provider.read"):
            raise RuntimeError("private failure details")

    telemetry = recorder.snapshot()
    span = telemetry.spans[0]
    assert telemetry.status == "failed"
    assert span.status == "error"
    assert span.error_kind == "RuntimeError"
    assert "private failure details" not in telemetry.model_dump_json()
    assert telemetry.summary().failed_span_count == 1


def test_failed_span_sanitizes_private_exception_class_names_without_replacing_error() -> None:
    class _PrivateFailure(Exception):
        pass

    recorder = ExecutionTelemetryRecorder()

    with pytest.raises(_PrivateFailure, match="original"):
        with recorder.span("provider.read"):
            raise _PrivateFailure("original")

    assert recorder.snapshot().spans[0].error_kind == "PrivateFailure"


def test_final_response_is_first_feedback_for_non_streamed_execution() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:final-only",
        telemetry_id="telemetry:final-only",
        clock_ns=_Clock(0, 4, 5),
        started_at_utc=_started_at(),
    )

    assert recorder.mark_final_response() == 4.0
    telemetry = recorder.snapshot()

    assert telemetry.status == "completed"
    assert telemetry.first_feedback_ms == 4.0
    assert telemetry.final_response_ms == 4.0
    assert telemetry.total_duration_ms == 5.0


def test_merge_is_idempotent_for_incremental_snapshots_and_summarizes_attempts() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:merge",
        telemetry_id="telemetry:merge",
        clock_ns=_Clock(0, 1, 2, 3, 4, 6, 7, 8, 9),
        started_at_utc=_started_at(),
        id_factory=_ids("attempt-one", "attempt-two"),
    )

    with recorder.span("sdk.model", attempt_index=1):
        pass
    first = recorder.snapshot()

    with recorder.span("sdk.model", attempt_index=2):
        pass
    recorder.mark_first_feedback()
    recorder.mark_final_response()
    second = recorder.snapshot()

    merged = first.merge(second, second)
    assert merged.status == "completed"
    assert len(merged.spans) == 2
    assert merged.first_feedback_ms == 7.0
    assert merged.final_response_ms == 8.0
    assert merged.total_duration_ms == 9.0
    assert merged.summary().attempt_count == 2


def test_merge_rejects_duplicate_attempt_with_a_different_immutable_span_id() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:merge-conflict",
        telemetry_id="telemetry:merge-conflict",
        clock_ns=_Clock(0, 1, 2, 3),
        started_at_utc=_started_at(),
        id_factory=_ids("original-span"),
    )
    with recorder.span("sdk.model"):
        pass
    original = recorder.snapshot()
    conflicting_span = original.spans[0].model_copy(update={"span_id": "conflicting-span"})
    conflicting = ExecutionTelemetry(
        telemetry_id=original.telemetry_id,
        run_id=original.run_id,
        started_at_utc=original.started_at_utc,
        status="running",
        total_duration_ms=original.total_duration_ms,
        spans=(conflicting_span,),
    )

    with pytest.raises(ValueError, match="Conflicting execution stage attempt"):
        original.merge(conflicting)


def test_contract_ids_and_spans_are_immutable() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:immutable",
        telemetry_id="telemetry:immutable",
        clock_ns=_Clock(0, 1, 2, 3),
        started_at_utc=_started_at(),
        id_factory=_ids("immutable-span"),
    )
    with recorder.span("sdk.model"):
        pass
    telemetry = recorder.snapshot()

    with pytest.raises(ValidationError, match="frozen"):
        telemetry.run_id = "run:changed"
    with pytest.raises(ValidationError, match="frozen"):
        telemetry.spans[0].span_id = "span:changed"


def test_snapshot_rejects_active_span_and_completed_status_without_final_response() -> None:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:active",
        telemetry_id="telemetry:active",
        clock_ns=_Clock(0, 1, 2, 3),
        started_at_utc=_started_at(),
        id_factory=_ids("active-span"),
    )

    with recorder.span("sdk.model"):
        with pytest.raises(RuntimeError, match="while a span is active"):
            recorder.snapshot()

    with pytest.raises(ValidationError, match="final-response marker"):
        recorder.snapshot(status="completed")
