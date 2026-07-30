from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from keystone_agents.execution_telemetry import ExecutionTelemetryRecorder
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
)
from keystone_agents.reporting import _agent_run_latency_label
from keystone_agents.run import _sdk_audit_output
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemEvent,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.trace_processor import record_sdk_run_summary_trace_event
from keystone_agents.workflow_runner import (
    _record_preflight_sdk_cost_events,
    _record_workflow_sdk_cost_event,
    _workflow_execution_steps,
)
from promptfoo.eval_database import list_eval_trace_events


class _Clock:
    def __init__(self, *milliseconds: int) -> None:
        self._values = iter(value * 1_000_000 for value in milliseconds)

    def __call__(self) -> int:
        return next(self._values)


def _telemetry_payload() -> dict:
    recorder = ExecutionTelemetryRecorder(
        run_id="run:projection",
        telemetry_id="telemetry:projection",
        clock_ns=_Clock(0, 10, 30, 50, 60),
        started_at_utc=datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
        id_factory=iter(["model-span"]).__next__,
    )
    with recorder.span(
        "sdk.model_attempt",
        attributes={"provider": "openai", "prompt": "private request body"},
    ):
        pass
    recorder.mark_final_response()
    return recorder.snapshot().model_dump(by_alias=True, mode="json")


def test_saved_sdk_audit_contains_only_compact_latency_projection() -> None:
    audit = _sdk_audit_output(
        output={"answer": "bounded"},
        model_provider="openai",
        model_name="gpt-test",
        model_run_mode="live_sdk",
        usage={},
        cost={},
        budget_guard={},
        request_cache={},
        execution_telemetry=_telemetry_payload(),
    )

    telemetry = audit["_execution_telemetry"]
    assert telemetry["final_response_ms"] == 50.0
    assert telemetry["stage_duration_ms"] == {"sdk.model_attempt": 20.0}
    assert "spans" not in telemetry
    assert "private request body" not in json.dumps(audit)


def test_missing_sdk_telemetry_preserves_legacy_audit_shape() -> None:
    audit = _sdk_audit_output(
        output={"answer": "bounded"},
        model_provider="openai",
        model_name="gpt-test",
        model_run_mode="live_sdk",
        usage={},
        cost={},
        budget_guard={},
        request_cache={},
    )
    assert "_execution_telemetry" not in audit


def test_work_item_sdk_usage_event_persists_compact_latency(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'telemetry.db'}")
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.IN_PROGRESS,
        title="Measure one SDK step",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)

    _record_workflow_sdk_cost_event(
        item,
        event_type="workflow_sdk_usage",
        summary="Recorded model usage.",
        agent_name="business_research_analyst",
        usage={"requests": 1},
        cost={},
        request_cache={},
        store=store,
        run_stage="research.synthesis",
        execution_telemetry=_telemetry_payload(),
    )

    metadata = store.list_work_item_events(item.id)[0].metadata
    telemetry = metadata["execution_telemetry"]
    assert telemetry["final_response_ms"] == 50.0
    assert telemetry["span_count"] == 1
    assert "spans" not in telemetry


def test_preflight_compaction_and_work_item_persistence_keep_only_summary(
    tmp_path: Path,
) -> None:
    compact = compact_orchestrator_preflight_payload(
        {
            "request_text": "Research a company",
            "sdk_usage_events": [
                {
                    "agent_name": "manual_request_planner",
                    "run_stage": "orchestrator_preflight.manual_request_planner",
                    "usage": {"requests": 1},
                    "execution_telemetry": _telemetry_payload(),
                }
            ],
        }
    )
    telemetry = compact["sdk_usage_events"][0]["execution_telemetry"]
    assert telemetry["total_duration_ms"] == 60.0
    assert "spans" not in telemetry
    assert "telemetry_id" not in telemetry

    store = SQLiteStore(f"sqlite:///{tmp_path / 'preflight.db'}")
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        status=WorkItemStatus.IN_PROGRESS,
        title="Measure preflight",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)
    _record_preflight_sdk_cost_events(
        item,
        request=WorkflowRunRequest(
            request_text="Research a company",
            orchestrator_preflight=compact,
        ),
        store=store,
    )
    saved = store.list_work_item_events(item.id)[0].metadata["execution_telemetry"]
    assert saved == telemetry


def test_workflow_execution_step_uses_compact_sdk_duration() -> None:
    steps = _workflow_execution_steps(
        [
            WorkItemEvent(event_type="advance_started"),
            WorkItemEvent(
                event_type="workflow_sdk_usage",
                metadata={
                    "usage": {"requests": 1},
                    "execution_telemetry": {"total_duration_ms": 875.0},
                },
            ),
        ]
    )
    assert steps[1].duration_ms == 875.0


def test_sdk_trace_summary_persists_compact_execution_timing(tmp_path: Path) -> None:
    database_path = tmp_path / "evals.sqlite"
    record_sdk_run_summary_trace_event(
        agent_name="business_research_analyst",
        execution_telemetry=_telemetry_payload(),
        database_path=database_path,
    )
    timing = list_eval_trace_events(database_path=database_path)[0]["metadata"][
        "execution_timing"
    ]
    assert timing["total_duration_ms"] == 60.0
    assert "spans" not in timing


def test_reporting_uses_final_response_latency_when_available() -> None:
    row = {
        "output_json": json.dumps(
            {"_execution_telemetry": {"final_response_ms": 1250.0}}
        )
    }
    assert _agent_run_latency_label(row) == "1.25s"


def test_reporting_prefers_total_sdk_duration() -> None:
    row = {
        "output_json": json.dumps(
            {
                "_execution_telemetry": {
                    "total_duration_ms": 1500.0,
                    "final_response_ms": 1250.0,
                }
            }
        )
    }
    assert _agent_run_latency_label(row) == "1.50s"
