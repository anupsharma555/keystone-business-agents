from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    UncertainOperation,
    execution_database_path,
    execution_lock,
)


def _receipt(object_id="rec_synthetic", verified=True):
    return {
        "status": "success" if verified else "partial_success",
        "provider": "airtable",
        "operation": "create_record",
        "record_id": object_id,
        "verification": {"passed": verified},
    }


def test_command_identity_deduplicates_delivery_not_identical_new_requests(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    first = store.begin({"request": "synthetic"}, origin="synthetic-event")
    assert store.begin({"request": "synthetic"}, origin="synthetic-event")["id"] == first["id"]
    assert store.begin({"request": "synthetic"})["id"] != first["id"]
    with pytest.raises(ExecutionConflict):
        store.begin({"request": "changed"}, origin="synthetic-event")


def test_known_unverified_and_unknown_effects_cannot_repeat(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    args = {"target": "synthetic", "value": 1}
    assert store.before_operation(run, "airtable_create_record", args) is None
    # A second process/handle sees the intent even before the first response.
    with pytest.raises(UncertainOperation):
        ExecutionStore(store.path).before_operation(run, "airtable_create_record", args)
    store.observe_operation(run, "airtable_create_record", args, _receipt(verified=False))
    with pytest.raises(UncertainOperation):
        store.before_operation(run, "airtable_create_record", args)
    with pytest.raises(ExecutionConflict):
        store.reconcile_operation(run, "airtable_create_record", args, _receipt("wrong_object"))
    store.reconcile_operation(run, "airtable_create_record", args, _receipt())
    assert (
        store.before_operation(run, "airtable_create_record", args)["record_id"] == "rec_synthetic"
    )
    assert store.before_operation(run, "airtable_create_record", {"target": "different"}) is None


@pytest.mark.parametrize("unresolved_status", ["intent", "observed", "unknown"])
def test_unresolved_write_blocks_changed_arguments_and_other_mutations(tmp_path, unresolved_status):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    original = {"target": "first"}
    store.before_operation(run, "airtable_create_record", original)
    if unresolved_status != "intent":
        store.observe_operation(
            run,
            "airtable_create_record",
            original,
            _receipt(verified=False) if unresolved_status == "observed" else None,
        )
    reopened = ExecutionStore(store.path)
    for tool_name in ("airtable_create_record", "google_doc_write"):
        with pytest.raises(UncertainOperation, match="unresolved"):
            reopened.before_operation(run, tool_name, {"target": "changed"})
    assert len(reopened.operations(run)) == 1
    assert reopened.operations(run)[0]["status"] == unresolved_status

    # Read-only reconciliation may establish the object before verifying it;
    # it never needs another provider mutation intent.
    reopened.observe_operation(run, "airtable_create_record", original, _receipt(verified=False))
    reopened.reconcile_operation(run, "airtable_create_record", original, _receipt())
    assert reopened.before_operation(run, "airtable_create_record", {"target": "second"}) is None
    reopened.observe_operation(
        run, "airtable_create_record", {"target": "second"}, _receipt("rec_second")
    )
    assert [row["status"] for row in reopened.operations(run)] == ["verified", "verified"]


def test_verified_exact_replay_remains_available_while_another_write_is_unresolved(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    first = {"target": "first"}
    store.before_operation(run, "airtable_create_record", first)
    store.observe_operation(run, "airtable_create_record", first, _receipt())
    store.before_operation(run, "airtable_create_record", {"target": "second"})
    assert (
        store.before_operation(run, "airtable_create_record", first)["record_id"] == "rec_synthetic"
    )
    with pytest.raises(UncertainOperation, match="unresolved"):
        store.before_operation(run, "airtable_create_record", {"target": "third"})
    assert len(store.operations(run)) == 2


def test_concurrent_distinct_writes_cannot_race_past_pending_intent(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    handles = [ExecutionStore(store.path), ExecutionStore(store.path)]
    start = Barrier(2)

    def admit(index):
        start.wait(timeout=5)
        try:
            handles[index].before_operation(run, "airtable_create_record", {"target": index})
        except UncertainOperation:
            return "blocked"
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, range(2)))
    assert sorted(outcomes) == ["admitted", "blocked"]
    assert len(store.operations(run)) == 1
    assert store.operations(run)[0]["status"] == "intent"


def test_sdk_read_tool_can_reconcile_while_new_mutations_are_blocked(tmp_path):
    from keystone_agents.receipts.journal import instrument_agent_tools
    from keystone_agents.runtime.durable_execution import activate_execution

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    original = {"target": "first"}
    store.before_operation(run, "airtable_create_record", original)
    store.observe_operation(run, "airtable_create_record", original, _receipt(verified=False))
    reads = []

    async def read_record(_context, arguments):
        reads.append(json.loads(arguments)["record_id"])
        return json.dumps({"status": "success", "record_id": "rec_synthetic"})

    tool = SimpleNamespace(name="airtable_get_record", on_invoke_tool=read_record)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    with activate_execution(store, run):
        output = json.loads(asyncio.run(tool.on_invoke_tool(None, '{"record_id":"rec_synthetic"}')))
        store.reconcile_operation(
            run, "airtable_create_record", original, _receipt(output["record_id"])
        )
        assert store.before_operation(run, "airtable_create_record", {"target": "second"}) is None
    assert reads == ["rec_synthetic"]
    assert [row["status"] for row in store.operations(run)] == ["verified", "intent"]


def test_model_allowance_survives_reopen_and_rejected_dispatch(tmp_path):
    path = tmp_path / "execution.sqlite3"
    store = ExecutionStore(path)
    run = store.begin({"request": "synthetic"}, budget_limit=2)["id"]
    assert store.reserve_model(run, "reason") == 1
    reopened = ExecutionStore(path)
    assert reopened.reserve_model(run, "review") == 2
    with pytest.raises(ExecutionConflict, match="budget exhausted"):
        reopened.reserve_model(run, "repeat")
    assert reopened.get(run)["consumed"] == 2


def test_execution_lock_is_released_after_forced_process_exit(tmp_path):
    lock = tmp_path / "worker.lock"
    code = (
        "import os; from pathlib import Path; "
        "from keystone_agents.runtime.durable_execution import execution_lock; "
        "lock=execution_lock(Path(__import__('sys').argv[1])); lock.__enter__(); os._exit(7)"
    )
    child = subprocess.run([sys.executable, "-c", code, str(lock)], check=False)
    assert child.returncode == 7
    with execution_lock(lock):
        with pytest.raises(ExecutionConflict, match="already running"):
            with execution_lock(lock):
                pytest.fail("two owners acquired one execution")


def test_delivery_reuses_exact_result_and_rejects_conflicting_ack(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    result = {"answer": "synthetic"}
    assert store.prepare_delivery(run, "synthetic-destination", result)["status"] == "pending"
    store.acknowledge_delivery(run, "synthetic-destination", result, "message-1")
    assert store.prepare_delivery(run, "synthetic-destination", result)["status"] == "delivered"
    with pytest.raises(ExecutionConflict):
        store.acknowledge_delivery(run, "synthetic-destination", result, "message-2")


def test_real_sqlite_graph_recovers_after_specialist_before_finalization(tmp_path, monkeypatch):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    import keystone_agents.langgraph_workflow as lg
    from keystone_agents.orchestration.checkpoints import (
        inspect_graph_execution,
        resume_graph_execution,
    )
    from keystone_agents.schemas.work_item import WorkflowRunRequest

    database = f"sqlite:///{tmp_path / 'business.db'}"
    request = WorkflowRunRequest(
        request_text="research Example Analytics",
        database_url=database,
        execution_id="synthetic-recovery",
    )
    calls = []
    original_specialist = lg._run_business_research_node
    original_finalize = lg._finalize_step_node

    def specialist(state):
        calls.append("research")
        return original_specialist(state)

    def fail_finalize(state):
        raise RuntimeError("synthetic crash before finalization")

    monkeypatch.setattr(lg, "_run_business_research_node", specialist)
    monkeypatch.setattr(lg, "_finalize_step_node", fail_finalize)
    with pytest.raises(RuntimeError, match="synthetic crash"):
        lg.run_work_item_langgraph(request)
    history = inspect_graph_execution("synthetic-recovery", database_url=database)
    assert history["history"][-1]["next_nodes"] == ["finalize_step"]
    assert calls == ["research"]
    monkeypatch.setattr(lg, "_finalize_step_node", original_finalize)
    outcome = resume_graph_execution("synthetic-recovery", database_url=database)
    assert outcome.durable is True
    assert calls == ["research"]
    detailed = inspect_graph_execution(
        request.execution_id, database_url=database, include_state=True
    )
    from keystone_agents.storage.sqlite_store import stable_hash

    assert all(stable_hash(row["state"]) == row["state_fingerprint"] for row in detailed["history"])
    saved_work_item = detailed["history"][-1]["state"]["result"]["work_item"]
    assert saved_work_item["id"] == outcome.result.work_item.id
    repeated = lg.run_work_item_langgraph(request)
    assert repeated.result.work_item.id == outcome.result.work_item.id
    assert calls == ["research"]


def test_graph_inspection_does_not_create_missing_database(tmp_path):
    from keystone_agents.orchestration.checkpoints import inspect_graph_execution

    database = f"sqlite:///{tmp_path / 'absent.db'}"
    with pytest.raises(ExecutionConflict):
        inspect_graph_execution("absent", database_url=database)
    assert not execution_database_path(database).exists()


def test_incompatible_checkpoint_schema_is_rejected(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    row = store.begin({"request": "synthetic"})
    with store.connection() as conn:
        conn.execute("UPDATE executions SET schema_name='future-version'")
    with pytest.raises(ExecutionConflict, match="schema"):
        store.get(row["id"])


def test_work_item_atomic_transition_rolls_back_artifacts_and_state(tmp_path):
    from keystone_agents.schemas.work_item import WorkItem, WorkItemKind
    from keystone_agents.storage.sqlite_store import SQLiteStore, stable_hash

    store = SQLiteStore(f"sqlite:///{tmp_path / 'business.db'}")
    item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="original")
    store.save_work_item(item)
    saved = store.get_work_item(item.id)
    version = stable_hash(saved.model_dump(mode="json"))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.save_work_item(
                saved.model_copy(update={"title": "rolled back"}), expected_version=version
            )
            raise RuntimeError("synthetic event failure")
    assert store.get_work_item(item.id).title == "original"
    store.save_work_item(saved.model_copy(update={"title": "new"}), expected_version=version)
    with pytest.raises(ValueError, match="changed since"):
        store.save_work_item(saved, expected_version=version)


def test_same_owner_phases_have_distinct_cached_results(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    run = store.begin({"request": "synthetic"})["id"]
    for phase in ["research:evidence", "research:decision", "research:review"]:
        store.save_stage(run, phase, {"revision": 1}, {"phase": phase})
    assert store.stage_result(run, "research:review", {"revision": 1}) == {
        "phase": "research:review"
    }
    assert store.stage_result(run, "research:review", {"revision": 2}) is None


def test_graph_resume_rejects_direct_execution_contract(tmp_path):
    from keystone_agents.orchestration.checkpoints import resume_graph_execution
    from keystone_agents.runtime.durable_execution import execution_database_path

    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    store = ExecutionStore(execution_database_path(database_url))
    execution_id = store.begin({"backend": "direct", "request": {}})["id"]
    store.update(execution_id, status="completed", result={"direct": "saved"})
    with pytest.raises(ExecutionConflict, match="no native graph contract"):
        resume_graph_execution(execution_id, database_url=database_url)


def test_journal_cli_reads_direct_operation_evidence_without_native_tables(tmp_path, capsys):
    from scripts.kba_execution import main

    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    store = ExecutionStore(execution_database_path(database_url))
    execution_id = store.begin({"backend": "direct", "request": {}}, budget_limit=2)["id"]
    store.before_operation(execution_id, "airtable_create_record", {"target": "synthetic"})
    before = store.path.read_bytes()
    assert main(["--database-url", database_url, "journal", execution_id]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["execution_id"] == execution_id
    assert output["budget_limit"] == 2
    assert output["consumed"] == 0
    assert output["operations"][0]["status"] == "intent"
    assert store.path.read_bytes() == before
