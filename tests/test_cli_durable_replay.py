"""Replay the exact CLI result, never an intermediate specialist output."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.runtime import durable_execution
from keystone_agents.runtime.durable_execution import ExecutionStore, execution_database_path
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _setup(tmp_path, *, json_output=True):
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    context = tmp_path / "slack-context.json"
    context.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "team_id": "T_SYNTHETIC",
                "channel_id": "C_SYNTHETIC",
                "request_ts": "1000.1",
            }
        )
    )
    args = SimpleNamespace(
        command="ask",
        database_url=database_url,
        json=json_output,
        agent="chief_of_staff",
        live_sdk=False,
        max_openai_requests=0,
        context_file=str(context),
    )
    return args, SQLiteStore(database_url)


def test_direct_duplicate_replays_final_envelope_not_specialist_row(tmp_path, capsys):
    args, store = _setup(tmp_path)
    run_id = store.save_agent_run(
        agent_name="chief_of_staff",
        output={"summary": "Intermediate specialist copy."},
        status="success",
    )
    calls = []

    def handler():
        cli._start_entry_execution_attempt(args, request_text="Summarize the synthetic note.")
        calls.append("executed")
        return cli._print_ask_live_payload(
            {
                "status": "done",
                "agent_run_id": run_id,
                "human_summary": "Final reviewed answer with its source evidence.",
                "source_ids": ["synthetic-source"],
                "send_enabled": False,
            },
            json_output=True,
        )

    assert cli._run_with_entry_telemetry(args, handler, structured_failure=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli._run_with_entry_telemetry(args, handler, structured_failure=True) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay.pop("reused_execution") is True
    assert replay == first
    assert calls == ["executed"]


@pytest.mark.parametrize("native_graph", [False, True])
def test_workitem_final_result_is_replayable_without_an_agent_run_link(
    tmp_path, capsys, native_graph
):
    args, store = _setup(tmp_path)
    calls = []

    def handler():
        cli._start_entry_execution_attempt(args, request_text="research Example Analytics")
        calls.append("executed")
        if native_graph:
            pytest.importorskip("langgraph.checkpoint.sqlite")
            from keystone_agents.langgraph_workflow import run_work_item_langgraph

            result = run_work_item_langgraph(
                WorkflowRunRequest(
                    request_text="research Example Analytics",
                    database_url=args.database_url,
                )
            ).result
        else:
            item = WorkItem(
                kind=WorkItemKind.RESEARCH_BRIEF,
                title="Synthetic note",
                status=WorkItemStatus.DONE,
                current_route=WorkItemRoute.CHIEF_OF_STAFF,
            )
            store.save_work_item(item)
            result = WorkflowRunResult(
                work_item=item,
                route=WorkItemRoute.CHIEF_OF_STAFF,
                status=WorkItemStatus.DONE,
                advanced=True,
                human_summary="The synthetic evidence is ready for review.",
            )
        return cli._print_work_item_result(result, json_output=True)

    assert cli._run_with_entry_telemetry(args, handler, structured_failure=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli._run_with_entry_telemetry(args, handler, structured_failure=True) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay.pop("reused_execution") is True
    assert replay == first
    assert calls == ["executed"]
    journal = ExecutionStore(execution_database_path(args.database_url), initialize=False)
    execution = journal.get(journal.list_executions()[0]["id"])
    if native_graph:
        assert "graph_runtime" in json.loads(execution["result_json"])
        result_id = first["work_item"]["id"]
        metadata = cli._stored_work_item_langgraph_metadata(store, result_id)
        assert metadata["execution_id"] == execution["id"]
        assert metadata["checkpoint_key"] == json.loads(execution["result_json"])["checkpoint_key"]
        assert journal.stage_result(execution["id"], "public_result", {}) == first


def test_plain_result_replay_preserves_final_text_and_excludes_progress(tmp_path, capsys):
    args, _ = _setup(tmp_path, json_output=False)
    calls = []

    def handler():
        cli._start_entry_execution_attempt(args, request_text="Summarize the synthetic note.")
        calls.append("executed")
        print("Progress that must not be replayed.")
        return cli._print_ask_live_payload(
            {
                "status": "done",
                "agent_name": "Synthetic reviewer",
                "human_summary": "Final reviewed answer.",
                "send_enabled": False,
            },
            json_output=False,
        )

    assert cli._run_with_entry_telemetry(args, handler) == 0
    first = capsys.readouterr().out.split("\n", 1)[1]
    assert cli._run_with_entry_telemetry(args, handler) == 0
    assert capsys.readouterr().out == first
    assert calls == ["executed"]


def test_completed_during_cli_lock_acquisition_is_rechecked(tmp_path, monkeypatch, capsys):
    args, _ = _setup(tmp_path)
    original_lock = durable_execution.execution_lock
    expected = {"status": "done", "human_summary": "Another invocation already completed this."}

    @contextmanager
    def completing_lock(path):
        with original_lock(path):
            journal = ExecutionStore(execution_database_path(args.database_url), initialize=False)
            execution_id = journal.list_executions()[0]["id"]
            journal.save_stage(execution_id, "public_result", {}, expected)
            journal.update(execution_id, status="completed")
            yield

    monkeypatch.setattr(durable_execution, "execution_lock", completing_lock)
    calls = []

    def handler():
        cli._start_entry_execution_attempt(args, request_text="Summarize the synthetic note.")
        calls.append("duplicate executed")
        return 0

    assert cli._run_with_entry_telemetry(args, handler) == 0
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {**expected, "reused_execution": True}


def test_bounded_json_fallback_selects_final_envelope_after_progress():
    payload = {"human_summary": "Reviewed result.", "output": {"value": "Text with {braces}."}}
    output = '{"stage":"progress"}\nWorking...\n' + json.dumps(payload, indent=2) + "\n"
    assert cli._final_json_result(output) == payload
    assert cli._final_json_result(output + "Still working\n") is None


def test_capture_limit_fails_closed_and_explicit_final_boundary_discards_progress():
    destination = io.StringIO()
    stream = cli._ObservedCLIStream(destination, cli.ExecutionTelemetryRecorder(), capture=True)
    stream.MAX_CAPTURE_CHARS = 10
    stream.write("Long progress output")
    assert stream.captured_text() is None
    stream.begin_final()
    stream.write("Done.\n")
    assert stream.captured_text() == "Done.\n"
    assert destination.getvalue() == "Long progress outputDone.\n"
