from __future__ import annotations

import json
import os
from types import SimpleNamespace

import keystone_agents.cli as cli
import keystone_agents.config as config_module
from keystone_agents.execution_telemetry import ExecutionTelemetryRecorder
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
