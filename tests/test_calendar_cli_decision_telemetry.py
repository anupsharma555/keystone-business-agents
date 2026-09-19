from __future__ import annotations

import json

import pytest

from keystone_agents import cli
from keystone_agents.agents.calendar_action_interpreter import (
    CalendarLookupAnswerResolution,
)
from keystone_agents.calendar_actions import CalendarActionPlan


@pytest.mark.parametrize(
    ("terminal_status", "attempt_count", "repair_attempted"),
    [
        ("accepted", 1, False),
        ("accepted", 2, True),
        ("rejected", 2, True),
    ],
)
def test_calendar_lookup_decision_telemetry_reaches_persistence_without_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal_status: str,
    attempt_count: int,
    repair_attempted: bool,
) -> None:
    private_event_id = "private-provider-event-id"
    attempts = [
        {
            "attempt": attempt,
            "status": "matched" if terminal_status == "accepted" else "ambiguous",
            "selected_event_indexes": [0],
            "selection_reason": "The title and normalized time match the request.",
            "validator_status": (
                "accepted"
                if terminal_status == "accepted" and attempt == attempt_count
                else "repair_required"
            ),
            "validator_feedback": (
                ""
                if terminal_status == "accepted" and attempt == attempt_count
                else "Choose only an index from the supplied candidate set."
            ),
            "tool_mode": "tool_free",
            "event_id": private_event_id,
        }
        for attempt in range(1, attempt_count + 1)
    ]
    decision_telemetry = {
        "schema": "keystone.calendar_lookup_decision.v1",
        "decision_owner": "calendar_lookup_synthesizer",
        "decision_stage": "calendar_verified_event_selection",
        "candidate_count": 2,
        "attempt_count": attempt_count,
        "repair_attempted": repair_attempted,
        "provider_calls_during_repair": 0,
        "model_tool_call_count": 0,
        "attempts": attempts,
        "terminal_status": terminal_status,
        "event_id": private_event_id,
    }
    base_tool_execution = {
        "schema": "keystone.tool_execution_summary.v1",
        "mode": "deterministic_bounded_provider_lifecycle",
        "workflow_called_tool_names": ["read_google_calendar_window"],
        "workflow_tool_call_count": 1,
        "provider_receipt_count": 1,
    }
    monkeypatch.setattr(
        cli,
        "execute_direct_calendar_action",
        lambda *_args, **_kwargs: {
            "status": "done",
            "selected_agent": "chief_of_staff",
            "tool_receipt": {
                "operation": "read_calendar_window",
                "verification": {"passed": True},
                "events": [
                    {
                        "event_id": private_event_id,
                        "title": "Synthetic interview",
                    }
                ],
            },
            "tool_execution": base_tool_execution,
            "human_summary": "Calendar read completed.",
            "send_enabled": False,
        },
    )
    monkeypatch.setattr(
        cli,
        "resolve_calendar_lookup_answer",
        lambda *_args, **_kwargs: CalendarLookupAnswerResolution(
            text="I found the matching synthetic interview.",
            response_scope="focused",
            status="matched" if terminal_status == "accepted" else "fallback",
            selected_event_indexes=(0,) if terminal_status == "accepted" else (),
            openai_requests=attempt_count,
            decision_telemetry=decision_telemetry,
        ),
    )
    persisted: dict[str, object] = {}

    class RecordingStore:
        def save_agent_run(self, **kwargs: object) -> int:
            persisted.update(kwargs)
            return 41

    monkeypatch.setattr(cli, "SQLiteStore", lambda *_args, **_kwargs: RecordingStore())

    exit_code = cli.run_direct_calendar_action(
        "List the matching interview tomorrow.",
        CalendarActionPlan(
            operation="read",
            read_scope="filtered_window",
            start_date="2026-08-04",
            end_date="2026-08-04",
            complete=True,
        ),
        live=True,
        json_output=False,
    )

    assert exit_code == 0
    output = persisted["output"]
    assert isinstance(output, dict)
    ownership = output["request_cache"]["decision_ownership"]
    assert ownership["terminal_status"] == terminal_status
    assert ownership["attempt_count"] == attempt_count
    assert ownership["repair_attempted"] is repair_attempted
    assert output["request_cache"]["decision_repairs"] == int(repair_attempted)
    assert output["request_cache"]["tool_execution"] == base_tool_execution
    assert output["decision_ownership"] == ownership
    assert private_event_id not in json.dumps(ownership, sort_keys=True)
    capsys.readouterr()


def test_calendar_lookup_decision_telemetry_omits_unrecognized_private_fields() -> None:
    safe = cli._safe_calendar_lookup_decision_telemetry(
        {
            "schema": "keystone.calendar_lookup_decision.v1",
            "decision_owner": "calendar_lookup_synthesizer",
            "terminal_status": "accepted",
            "provider_event_id": "private-event-id",
            "attempts": [
                {
                    "attempt": 1,
                    "status": "matched",
                    "selected_event_indexes": [0],
                    "provider_event_id": "private-event-id",
                }
            ],
        }
    )

    assert "provider_event_id" not in safe
    assert "provider_event_id" not in safe["attempts"][0]
    assert "private-event-id" not in json.dumps(safe, sort_keys=True)
