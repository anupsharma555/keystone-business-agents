"""Recovery must close only the exact abandoned CLI audit attempt."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.runtime.durable_execution import ExecutionConflict, ExecutionStore
from keystone_agents.runtime.execution_attempt import (
    bind_execution_attempt,
    record_execution_attempt_recovery,
    start_execution_attempt,
)
from keystone_agents.storage.sqlite_store import SQLiteStore

_DRIVER = r"""
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from keystone_agents.entrypoints import cli_impl as cli
import keystone_agents.langgraph_workflow as graph
import keystone_agents.orchestration.checkpoints as checkpoints
import keystone_agents.runtime.provenance as provenance
from keystone_agents.runtime.durable_execution import ExecutionStore, execution_database_path
from keystone_agents.runtime.execution_attempt import start_execution_attempt
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.storage.sqlite_store import SQLiteStore
root, mode = Path(sys.argv[1]), sys.argv[2]
database = 'sqlite:///' + str(root / 'business.db')
fingerprint = lambda: {'runtime_sha256': 'synthetic-audit-revision'}
provenance.current_runtime_fingerprint = fingerprint
checkpoints.current_runtime_fingerprint = fingerprint
request_text = 'research Example Analytics'
request = WorkflowRunRequest(
    request_text=request_text, database_url=database,
    orchestrator_preflight={'sdk_usage_events': [{
        'agent_name': 'orchestrator',
        'usage': {'available': True, 'requests': 2, 'input_tokens': 17, 'output_tokens': 5},
        'cost': {'estimated_usd': 0.00004},
    }]},
)
original = graph._run_business_research_node
def specialist(state):
    with (root / 'calls.txt').open('a') as handle: handle.write('research\n')
    return original(state)
graph._run_business_research_node = specialist
if mode in {'crash', 'legacy-crash'}:
    graph._finalize_step_node = lambda state: os._exit(13)
    if mode == 'legacy-crash':
        start_execution_attempt(store=SQLiteStore(database), request_text=request_text, live=False)
        graph.run_work_item_langgraph(request)
    else:
        context = root / 'origin.json'
        context.write_text(json.dumps({'schema': 'keystone.slack.history_context.v1',
            'team_id': 'T_SYNTHETIC', 'channel_id': 'C_SYNTHETIC', 'request_ts': '1000.1'}))
        args = SimpleNamespace(command='ask', database_url=database, agent='orchestrator',
                               live_sdk=False, json=True, max_openai_requests=0,
                               context_file=str(context))
        def run():
            cli._start_entry_execution_attempt(args, request_text=request_text)
            outcome = graph.run_work_item_langgraph(request)
            return cli._print_work_item_result(outcome.result, json_output=True)
        cli._run_with_entry_telemetry(args, run, structured_failure=True)
else:
    if mode == 'failed-resume':
        def fail(state): raise RuntimeError('Synthetic recovery failure')
        graph._finalize_step_node = fail
    store = ExecutionStore(execution_database_path(database), initialize=False)
    execution_id = store.list_executions()[0]['id']
    try:
        outcome = checkpoints.resume_graph_execution(execution_id, database_url=database)
    except RuntimeError as exc:
        print(type(exc).__name__)
        raise SystemExit(5)
    print(json.dumps({'status': outcome.result.status.value, 'execution_id': execution_id}))
"""


def _run(root, mode):
    environment = {
        key: value for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
    }
    environment.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
        PYTHON_DOTENV_DISABLED="1", KEYSTONE_DRY_RUN="true", KEYSTONE_LIVE_MODE="false",
        DATABASE_URL=f"sqlite:///{root / 'business.db'}",
    )
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, str(root), mode], env=environment,
        capture_output=True, text=True, timeout=40,
    )


@pytest.mark.parametrize("failed_recovery", [False, True])
def test_killed_cli_attempt_is_interrupted_and_exactly_linked_to_native_recovery(
    tmp_path, failed_recovery, capsys
):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    crashed = _run(tmp_path, "crash")
    assert crashed.returncode == 13, crashed.stderr
    business = SQLiteStore(f"sqlite:///{tmp_path / 'business.db'}")
    journal = ExecutionStore(tmp_path / "business.db.execution.sqlite3", initialize=False)
    execution_id = journal.list_executions()[0]["id"]
    link = journal.stage_result(execution_id, "entry_attempt", {})
    original = business.get_agent_run(link["attempt_id"])
    assert original["status"] == "started"
    assert original["output"]["durable_execution_id"] == execution_id
    # Same wording is insufficient identity: this different request must stay untouched.
    other = start_execution_attempt(
        store=business, request_text="research Example Analytics", live=False,
    )
    unrelated = business.get_agent_run(other.run_id)
    preflight = journal.stage_result(execution_id, "graph_contract", {})["request"][
        "orchestrator_preflight"
    ]
    assert preflight["sdk_usage_events"][0]["usage"]["input_tokens"] == 17
    assert preflight["sdk_usage_events"][0]["cost"]["estimated_usd"] == 0.00004
    if failed_recovery:
        failed = _run(tmp_path, "failed-resume")
        assert failed.returncode == 5, failed.stderr
        interrupted = business.get_agent_run(link["attempt_id"])
        assert interrupted["status"] == "interrupted"
        assert interrupted["output"]["native_graph_recovery"]["status"] == "failed"
        assert journal.get(execution_id)["status"] == "failed"
        assert business.get_agent_run(other.run_id) == unrelated
    resumed = _run(tmp_path, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["status"] == "in_progress"
    assert journal.get(execution_id)["status"] == "completed"
    settled = business.get_agent_run(link["attempt_id"])
    assert settled["status"] == settled["output"]["terminal"]["status"] == "interrupted"
    recovery = settled["output"]["native_graph_recovery"]
    assert recovery["status"] == "completed"
    assert recovery["original_cli_exit_code"] is None
    assert recovery["original_cli_response_delivery"] == "unverified"
    if failed_recovery:
        assert "failed" in [event["status"] for event in recovery["observations"]]
    assert "exit_code" not in settled["output"]["terminal"]
    assert business.get_agent_run(other.run_id) == unrelated
    assert (tmp_path / "calls.txt").read_text() == "research\n"
    assert journal.stage_result(execution_id, "graph_contract", {})["request"][
        "orchestrator_preflight"
    ] == preflight
    saved = journal.stage_result(execution_id, "public_result", {})
    text = journal.stage_result(execution_id, "public_text", {})["text"]
    assert saved["recovery"]["render_model_requests"] == 0
    assert saved["recovery"]["original_cli_response_delivery"] == "unverified"
    # A completed native invocation does not turn an unfinished WorkItem into done.
    assert saved["completion_confirmed"] is False
    final = json.loads(journal.get(execution_id)["result_json"])["result"]
    assert saved["human_summary"] == final["human_summary"]
    args = SimpleNamespace(
        command="ask", database_url=business.database_url, agent="orchestrator",
        live_sdk=False, json=True, max_openai_requests=0,
        context_file=str(tmp_path / "origin.json"),
    )

    def repeated_delivery():
        cli._start_entry_execution_attempt(args, request_text="research Example Analytics")
        pytest.fail("duplicate delivery reran its business handler")

    assert cli._run_with_entry_telemetry(args, repeated_delivery, structured_failure=True) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay.pop("reused_execution") is True
    assert replay == saved
    args.json = False
    assert cli._run_with_entry_telemetry(args, repeated_delivery, structured_failure=True) == 0
    assert capsys.readouterr().out == text
    assert _run(tmp_path, "resume").returncode == 0
    assert business.get_agent_run(link["attempt_id"]) == settled
    with journal.connection() as connection:
        connection.execute(
            "DELETE FROM execution_stages WHERE execution_id=? AND stage_id='public_text'",
            (execution_id,),
        )
    assert _run(tmp_path, "resume").returncode == 0
    assert journal.stage_result(execution_id, "public_result", {}) == saved
    assert journal.stage_result(execution_id, "public_text", {})["text"] == text
    assert (tmp_path / "calls.txt").read_text() == "research\n"


def test_legacy_resume_does_not_guess_a_matching_original_cli_attempt(tmp_path):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    assert _run(tmp_path, "legacy-crash").returncode == 13
    business = SQLiteStore(f"sqlite:///{tmp_path / 'business.db'}")
    before = business.get_agent_run(1)
    assert before["status"] == "started"
    resumed = _run(tmp_path, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert business.get_agent_run(1) == before


def test_same_wording_cannot_substitute_another_attempt_in_a_forged_link(tmp_path):
    database = f"sqlite:///{tmp_path / 'business.db'}"
    business = SQLiteStore(database)
    first = start_execution_attempt(store=business, request_text="Same request", live=False)
    second = start_execution_attempt(store=business, request_text="Same request", live=False)
    link = bind_execution_attempt(first, "synthetic-one")
    bind_execution_attempt(second, "synthetic-two")
    before = business.get_agent_runs([first.run_id, second.run_id])
    with pytest.raises(ExecutionConflict, match="does not match"):
        record_execution_attempt_recovery(
            database_url=database, execution_id="synthetic-one",
            request_fingerprint=first.request_fingerprint,
            link={**link, "attempt_id": second.run_id}, recovery_status="completed",
        )
    assert business.get_agent_runs([first.run_id, second.run_id]) == before


@pytest.mark.parametrize("terminal_status", ["completed", "failed"])
def test_a_previously_finalized_original_attempt_is_not_rewritten(tmp_path, terminal_status):
    database = f"sqlite:///{tmp_path / 'business.db'}"
    business = SQLiteStore(database)
    attempt = start_execution_attempt(store=business, request_text="Same request", live=False)
    link = bind_execution_attempt(attempt, "synthetic-one")
    attempt.finalize(status=terminal_status, exit_code=0 if terminal_status == "completed" else 1)
    before = business.get_agent_run(attempt.run_id)
    assert before["output"]["durable_execution_id"] == "synthetic-one"
    record_execution_attempt_recovery(
        database_url=database, execution_id="synthetic-one",
        request_fingerprint=attempt.request_fingerprint, link=link, recovery_status="completed",
    )
    assert business.get_agent_run(attempt.run_id) == before
