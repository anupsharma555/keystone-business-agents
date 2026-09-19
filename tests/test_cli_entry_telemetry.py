from __future__ import annotations

import json
import os
from types import SimpleNamespace

import keystone_agents.cli as cli
import keystone_agents.config as config_module
from keystone_agents.execution_telemetry import ExecutionTelemetryRecorder
from keystone_agents.runtime.execution_attempt import start_execution_attempt
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


class _Clock:
    def __init__(self, *milliseconds: int) -> None:
        self._values = iter(value * 1_000_000 for value in milliseconds)

    def __call__(self) -> int:
        return next(self._values)


def _args(*, database_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(live_sdk=False, database_url=database_url)


def test_ask_entrypoint_preserves_stdout_and_persists_final_timing(
    monkeypatch,
    capsys,
) -> None:
    persisted: list[dict] = []
    expected = {
        "status": "completed",
        "human_summary": "Authorized work content.",
    }

    def fake_ask(_args) -> int:
        print(json.dumps(expected, sort_keys=True))
        return 0

    monkeypatch.setattr(cli, "_run_ask_with_current_environment", fake_ask)
    monkeypatch.setattr(
        cli,
        "_persist_entry_execution_telemetry",
        lambda _scope, telemetry: persisted.append(telemetry),
    )

    exit_code = cli._run_ask(_args())

    assert exit_code == 0
    assert capsys.readouterr().out == json.dumps(expected, sort_keys=True) + "\n"
    assert len(persisted) == 1
    telemetry = persisted[0]
    assert telemetry["schema"] == "keystone.execution_telemetry_summary.v1"
    assert telemetry["status"] == "completed"
    assert telemetry["first_feedback_ms"] is not None
    assert telemetry["final_response_ms"] >= telemetry["first_feedback_ms"]
    assert telemetry["stage_duration_ms"]["entry.dispatch"] >= 0
    assert "Authorized work content" not in json.dumps(telemetry)
    assert cli._ASK_ENTRY_TELEMETRY.get() is None


def test_first_delegated_write_precedes_flushed_final_response(
    monkeypatch,
    capsys,
) -> None:
    recorder = ExecutionTelemetryRecorder(
        clock_ns=_Clock(0, 10, 20, 30, 40, 50, 60),
    )
    persisted: list[dict] = []
    monkeypatch.setattr(cli, "ExecutionTelemetryRecorder", lambda: recorder)
    monkeypatch.setattr(
        cli,
        "_persist_entry_execution_telemetry",
        lambda _scope, telemetry: persisted.append(telemetry),
    )

    exit_code = cli._run_with_entry_telemetry(
        _args(),
        lambda: (print("first feedback"), 0)[1],
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "first feedback\n"
    telemetry = persisted[0]
    assert telemetry["first_feedback_ms"] == 20.0
    assert telemetry["final_response_ms"] == 50.0


def test_explicit_work_item_entry_owns_the_same_output_observer(
    monkeypatch,
    capsys,
) -> None:
    persisted: list[dict] = []

    def fake_advance(_args) -> int:
        assert cli._ASK_ENTRY_TELEMETRY.get() is not None
        print("work item complete")
        return 0

    monkeypatch.setattr(
        cli,
        "_run_work_items_advance_with_current_environment",
        fake_advance,
    )
    monkeypatch.setattr(
        cli,
        "_persist_entry_execution_telemetry",
        lambda _scope, telemetry: persisted.append(telemetry),
    )

    exit_code = cli._run_work_items_advance(_args())

    assert exit_code == 0
    assert capsys.readouterr().out == "work item complete\n"
    assert persisted[0]["status"] == "completed"
    assert cli._ASK_ENTRY_TELEMETRY.get() is None


def test_live_work_item_entry_loads_and_restores_repo_environment(
    monkeypatch,
    capsys,
) -> None:
    loads: list[tuple[str, bool]] = []
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)

    def fake_load_settings(
        env_file: str = ".env",
        *,
        force_dotenv: bool = False,
    ) -> SimpleNamespace:
        loads.append((env_file, force_dotenv))
        os.environ["KEYSTONE_OPENAI_API_KEY"] = "test-only-kba-key"
        return SimpleNamespace()

    def fake_advance(_args) -> int:
        assert os.environ["KEYSTONE_OPENAI_API_KEY"] == "test-only-kba-key"
        print("live work item complete")
        return 0

    monkeypatch.setattr(config_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(
        cli,
        "_run_work_items_advance_with_current_environment",
        fake_advance,
    )
    monkeypatch.setattr(
        cli,
        "_persist_entry_execution_telemetry",
        lambda _scope, _telemetry: None,
    )
    args = _args()
    args.live_sdk = True

    assert cli._run_work_items_advance(args) == 0
    assert capsys.readouterr().out == "live work item complete\n"
    assert loads == [(".env", True)]
    assert "KEYSTONE_OPENAI_API_KEY" not in os.environ


def test_work_item_entry_timing_is_persisted_as_content_free_event(
    tmp_path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'entry-telemetry.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Internal architecture review",
    )
    store.save_work_item(item)
    result = WorkflowRunResult(
        work_item=item.model_copy(update={"status": WorkItemStatus.DONE}),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="The review found one bounded result.",
    )

    exit_code = cli._run_with_entry_telemetry(
        _args(database_url=database_url),
        lambda: cli._print_work_item_result(result, json_output=True),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert "_entry_execution_telemetry" not in payload
    events = store.list_work_item_events(item.id)
    event = next(event for event in events if event.event_type == "entrypoint_execution_telemetry")
    telemetry = event.metadata["execution_telemetry"]
    assert telemetry["status"] == "completed"
    assert "Internal architecture review" not in json.dumps(telemetry)


def test_direct_agent_entry_timing_updates_internal_run_only(
    tmp_path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'agent-entry-telemetry.db'}"
    store = SQLiteStore(database_url)
    run_id = store.save_agent_run(
        agent_name="chief_of_staff",
        input_summary="Sensitive work request",
        output={"human_summary": "Bounded answer."},
        dry_run=False,
    )

    exit_code = cli._run_with_entry_telemetry(
        _args(database_url=database_url),
        lambda: cli._print_ask_live_payload(
            {
                "status": "completed",
                "agent_name": "Chief of Staff",
                "agent_run_id": run_id,
                "human_summary": "Bounded answer.",
                "send_enabled": False,
            },
            json_output=True,
        ),
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert "_entry_execution_telemetry" not in payload
    row = next(row for row in store.fetch_all("agent_runs") if row["id"] == run_id)
    stored = json.loads(row["output_json"])
    telemetry = stored["_entry_execution_telemetry"]
    assert telemetry["status"] == "completed"
    assert "Sensitive work request" not in json.dumps(telemetry)


def test_ask_preflight_failure_is_persisted_and_rendered_without_traceback(
    tmp_path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-attempt.db'}"
    args = SimpleNamespace(
        command="ask",
        live_sdk=False,
        database_url=database_url,
        json=True,
        agent="chief_of_staff",
    )
    ModelBehaviorError = type("ModelBehaviorError", (RuntimeError,), {})

    def fail_before_specialist() -> int:
        cli._start_entry_execution_attempt(
            args,
            request_text="Private natural-language request SHOULD_NOT_BE_STORED.",
        )
        raise ModelBehaviorError(
            "ValidationError: needs_more_context cannot also claim a selection"
        )

    exit_code = cli._run_with_entry_telemetry(
        args,
        fail_before_specialist,
        structured_failure=True,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload["schema"] == "keystone.execution_failure.v1"
    assert payload["failure"]["kind"] == "schema_or_parse_error"
    assert payload["side_effects"]["external_write_state"] == "unknown"
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    assert "Business Agents run failed with structured diagnostics." in captured.err
    assert "retained internally" not in captured.err
    store = SQLiteStore(database_url)
    rows = store.fetch_all("agent_runs")
    assert len(rows) == 1
    assert rows[0]["agent_name"] == "kba_entrypoint"
    assert rows[0]["status"] == "failed"
    stored = json.loads(rows[0]["output_json"])
    assert stored["terminal"]["status"] == "failed"
    assert stored["failure"]["kind"] == "schema_or_parse_error"
    assert stored["internal_diagnostics"]["raw_traceback_retained"] is False
    serialized = json.dumps(rows)
    assert "SHOULD_NOT_BE_STORED" not in serialized
    assert "needs_more_context" not in serialized


def test_ask_attempt_links_every_explicit_manager_and_specialist_run(
    tmp_path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-multi-stage.db'}"
    store = SQLiteStore(database_url)

    def linked_output(stage: str) -> dict[str, object]:
        return {
            "_sdk_usage": {
                "available": True,
                "requests": 1,
                "provider_request_count_confirmed": True,
            },
            "_sdk_cost": {"available": True, "estimated_usd": 0.0001},
            "_sdk_request_cache": {
                "dynamic_prompt_sha256": ("a" if stage == "manager" else "b") * 64,
                "dynamic_prompt_chars": 80,
                "tool_execution": {
                    "mode": "tool_free",
                    "model_tool_call_count": 0,
                    "model_called_tool_names": [],
                    "workflow_tool_call_count": 0,
                    "workflow_called_tool_names": [],
                    "workflow_called_helper_names": [],
                    "preacquired_context_tool_names": [],
                    "provider_request_attempt_count": 0,
                    "provider_request_attempt_count_available": True,
                    "provider_request_success_count": 0,
                    "provider_request_success_count_available": True,
                    "provider_receipt_count": 0,
                    "provider_receipt_count_available": True,
                },
                "decision_ownership": {
                    "decision_owner": (
                        "orchestrator" if stage == "manager" else "specialist_agent"
                    ),
                    "decision_stage": f"{stage}_decision",
                    "attempts": [
                        {
                            "attempt": 1,
                            "candidate_ids": [f"{stage}-candidate"],
                            "selected_candidate_ids": [f"{stage}-candidate"],
                            "validator_outcome": {"status": "accepted"},
                        }
                    ],
                },
            },
        }

    manager_id = store.save_agent_run(
        agent_name="orchestrator",
        output={
            **linked_output("manager"),
            "handoffs": [
                {
                    "source_agent": "orchestrator",
                    "downstream_agent": "business_research_analyst",
                    "provided": True,
                    "consumed": True,
                    "consumption_source": "typed_context_pack",
                    "evidence_ids": ["private-handoff-id"],
                }
            ],
        },
        status="success",
    )
    specialist_id = store.save_agent_run(
        agent_name="business_research_analyst",
        output=linked_output("specialist"),
        status="success",
    )
    args = SimpleNamespace(
        command="ask",
        live_sdk=False,
        database_url=database_url,
        json=True,
        agent="orchestrator",
        max_openai_requests=2,
    )

    def run_linked_payload() -> int:
        cli._start_entry_execution_attempt(args, request_text="Private request not retained.")
        return cli._print_ask_live_payload(
            {
                "status": "completed",
                "manager": {"agent_run_id": manager_id},
                "specialist": {"agent_run_id": specialist_id},
                "send_enabled": False,
            },
            json_output=True,
        )

    assert cli._run_with_entry_telemetry(args, run_linked_payload) == 0
    capsys.readouterr()

    rows = store.fetch_all("agent_runs")
    attempt_row = next(row for row in rows if row["agent_name"] == "kba_entrypoint")
    attempt = json.loads(attempt_row["output_json"])
    assert attempt["links"]["agent_run_ids"] == [manager_id, specialist_id]
    stages = attempt["decision_trace"]["stages"]
    assert [stage["run_id"] for stage in stages] == [
        str(manager_id),
        str(specialist_id),
    ]
    assert stages[0]["handoffs"][0]["consumed"] is True
    assert {
        key: attempt["request_budget"][key]
        for key in (
            "model_request_ceiling",
            "ceiling_source",
            "admission_gate",
            "observed_model_requests",
            "observed_count_confirmed",
            "within_ceiling",
            "status",
        )
    } == {
        "model_request_ceiling": 2,
        "ceiling_source": "cli_max_openai_requests",
        "admission_gate": "ask_entrypoint_estimated_request_maximum",
        "observed_model_requests": 2,
        "observed_count_confirmed": False,
        "within_ceiling": True,
        "status": "within_ceiling",
    }
    assert attempt["request_budget"]["limit"] == 2
    assert attempt["request_budget"]["consumed"] == 0
    # These synthetic linked rows predate this empty request ledger; their usage
    # must not be mislabeled as a reconciled total for the current attempt.
    assert attempt["request_budget"]["usage_reconciled_with_ledger"] is False
    assert "request_usage_reconciliation_incomplete" in attempt["unavailable_evidence"]
    assert attempt["request_budget"]["remaining"] == 2
    assert attempt["request_budget"]["exhausted"] is False
    assert attempt["request_budget"]["exhaustion_stage"] == ""
    assert attempt["request_budget"]["enforcement"] == "pre_model_invocation"
    assert attempt["request_budget"]["correlation_id"]
    serialized = json.dumps(attempt)
    assert "private-handoff-id" not in serialized
    assert "Private request not retained" not in serialized


def test_execution_attempt_marks_observed_request_ceiling_violation(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-request-ceiling.db'}"
    store = SQLiteStore(database_url)
    specialist_id = store.save_agent_run(
        agent_name="rss_context_agent",
        output={
            "_sdk_usage": {
                "available": True,
                "requests": 2,
                "provider_request_count_confirmed": True,
            },
            "_sdk_cost": {"available": True, "estimated_usd": 0.001},
            "_sdk_request_cache": {
                "dynamic_prompt_sha256": "a" * 64,
                "dynamic_prompt_chars": 80,
                "tool_execution": {
                    "mode": "tool_free",
                    "model_tool_call_count": 0,
                    "model_called_tool_names": [],
                    "workflow_tool_call_count": 0,
                    "workflow_called_tool_names": [],
                    "workflow_called_helper_names": [],
                    "preacquired_context_tool_names": [],
                    "provider_request_attempt_count": 0,
                    "provider_request_attempt_count_available": True,
                    "provider_request_success_count": 0,
                    "provider_request_success_count_available": True,
                    "provider_receipt_count": 0,
                    "provider_receipt_count_available": True,
                },
                "decision_ownership": {
                    "decision_owner": "specialist_agent",
                    "decision_stage": "signal_relevance_selection",
                    "attempts": [
                        {
                            "attempt": 1,
                            "candidate_ids": [],
                            "selected_candidate_ids": [],
                            "validator_outcome": {"status": "accepted"},
                        }
                    ],
                },
            },
        },
        status="success",
    )
    attempt = start_execution_attempt(
        store=store,
        request_text="Private bounded request.",
        route_hint="rss_context_agent",
        live=True,
        max_model_requests=1,
    )

    payload = attempt.finalize(
        status="completed",
        exit_code=0,
        linked_agent_run_id=specialist_id,
    )

    assert payload["request_budget"]["status"] == "exceeded"
    assert payload["request_budget"]["within_ceiling"] is False
    assert payload["decision_trace"]["evaluation_status"] == "fail"
    assert "model_request_ceiling_exceeded" in payload["decision_trace"]["findings"]
