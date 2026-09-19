"""Uncertain external effects follow a WorkItem across execution backends."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.journal import instrument_agent_tools, record_provider_observation
from keystone_agents.runtime.durable_execution import (
    ExecutionStore,
    UncertainOperation,
    activate_execution,
    current_execution,
    execution_database_path,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _receipt(*, verified=True):
    return {
        "status": "success" if verified else "partial_success",
        "provider": "airtable", "operation": "create_record",
        "record_id": "rec_synthetic", "verification": {"passed": verified},
    }


def _begin(store, identity, work_item_id=""):
    store.begin({"request": "synthetic"}, execution_id=identity)
    store.update(identity, status="running", work_item_id=work_item_id)
    return identity


@pytest.mark.parametrize("status", ["intent", "observed", "unknown"])
def test_unresolved_effect_blocks_new_execution_only_on_the_same_work_item(tmp_path, status):
    store = ExecutionStore(tmp_path / "execution.db")
    old = _begin(store, "old", "wi_shared")
    new = _begin(store, "new", "wi_shared")
    other = _begin(store, "other", "wi_other")
    arguments = {"value": "original"}
    store.before_operation(old, "airtable_create_record", arguments)
    if status != "intent":
        store.observe_operation(
            old, "airtable_create_record", arguments,
            _receipt(verified=False) if status == "observed" else None,
        )
    reopened = ExecutionStore(store.path)
    with pytest.raises(UncertainOperation, match="unresolved") as caught:
        reopened.before_operation(new, "google_doc_write", {"value": "new"})
    assert f'"{old}"' in str(caught.value)
    assert "original" not in str(caught.value)
    assert reopened.operations(new) == []
    assert reopened.before_operation(other, "google_doc_write", {"value": "new"}) is None

    # A read-only reconciliation establishes and verifies the old object.
    reopened.observe_operation(old, "airtable_create_record", arguments, _receipt(verified=False))
    reopened.reconcile_operation(old, "airtable_create_record", arguments, _receipt())
    assert reopened.before_operation(new, "google_doc_write", {"value": "new"}) is None


def test_unbound_executions_do_not_create_a_global_mutation_barrier(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    for identity, work_item_id in [("unbound1", ""), ("unbound2", ""), ("bound", "wi_other")]:
        _begin(store, identity, work_item_id)
        assert store.before_operation(
            identity, "airtable_create_record", {"value": identity},
        ) is None


def test_verified_replay_and_read_only_tools_remain_available_across_the_barrier(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    old = _begin(store, "old", "wi_shared")
    new = _begin(store, "new", "wi_shared")
    verified = {"value": "verified"}
    unknown = {"value": "unknown"}
    store.before_operation(new, "airtable_create_record", verified)
    store.observe_operation(new, "airtable_create_record", verified, _receipt())
    store.before_operation(old, "airtable_create_record", unknown)
    store.observe_operation(old, "airtable_create_record", unknown, _receipt(verified=False))

    reads = []

    async def read_record(_context, _arguments):
        reads.append("read")
        return json.dumps(_receipt())

    tool = SimpleNamespace(name="airtable_get_record", on_invoke_tool=read_record)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    with activate_execution(store, new):
        assert store.before_operation(new, "airtable_create_record", verified) == _receipt()
        observed = json.loads(asyncio.run(
            tool.on_invoke_tool(None, '{"record_id":"rec_synthetic"}'),
        ))
        store.reconcile_operation(old, "airtable_create_record", unknown, observed)
        assert store.before_operation(new, "airtable_create_record", {"value": "next"}) is None
    assert reads == ["read"]


def test_separate_executions_cannot_concurrently_admit_two_uncertain_work_item_writes(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identities = [_begin(store, f"execution-{index}", "wi_shared") for index in range(2)]
    barrier = Barrier(2)

    def admit(identity):
        handle = ExecutionStore(store.path)
        barrier.wait(timeout=10)
        try:
            handle.before_operation(identity, "airtable_create_record", {"value": identity})
        except UncertainOperation:
            return "blocked"
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, identities)) == ["admitted", "blocked"]
    assert sum(len(store.operations(identity)) for identity in identities) == 1


@pytest.mark.parametrize("backend", ["direct", "graph"])
@pytest.mark.parametrize("existing_item", [False, True])
def test_failed_advance_binds_primary_work_item_before_fake_provider_effect(
    tmp_path, monkeypatch, backend, existing_item,
):
    import keystone_agents.langgraph_workflow as graph
    import keystone_agents.workflow_runner as workflow

    if backend == "graph":
        pytest.importorskip("langgraph.checkpoint.sqlite")
    database = f"sqlite:///{tmp_path / 'business.db'}"
    business = SQLiteStore(database)
    item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic research",
                    current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST)
    if existing_item:
        business.save_work_item(item)
    expected_work_item = []
    effects = []
    fail_after_effect = True

    async def fake_provider(_context, _arguments):
        execution = current_execution()
        bound = execution.store.get(execution.execution_id)["work_item_id"]
        assert bound == expected_work_item[-1]
        assert business.get_work_item(bound) is not None
        effects.append(bound)
        if fail_after_effect:
            record_provider_observation(_receipt(verified=False))
            raise ConnectionError("Synthetic read-back failure after provider create")
        return json.dumps(_receipt())

    tool = SimpleNamespace(name="airtable_create_record", on_invoke_tool=fake_provider)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))

    def specialist(prepared):
        expected_work_item.append(prepared.work_item.id)
        asyncio.run(tool.on_invoke_tool(None, '{"value":"synthetic"}'))
        pytest.fail("Synthetic provider should fail or be blocked before returning.")

    monkeypatch.setattr(workflow, "run_prepared_work_item_specialist", specialist)
    monkeypatch.setattr(graph, "_run_business_research_node", lambda state: specialist(
        graph._prepared_step_from_state(state),
    ))
    request = WorkflowRunRequest(
        request_text="Research Example Analytics", database_url=database,
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        work_item_id=item.id if existing_item else None,
        execution_id="first-execution", live_sdk=False, live_search=False,
    )
    with pytest.raises(ConnectionError, match="read-back failure"):
        if backend == "direct":
            workflow.advance_work_item(request)
        else:
            graph.run_work_item_langgraph(request)

    journal = ExecutionStore(execution_database_path(database))
    primary = journal.get("first-execution")["work_item_id"]
    assert primary == expected_work_item[-1]
    assert primary
    assert journal.operations("first-execution")[0]["status"] == "observed"

    # A fresh direct execution (also covering graph -> direct) cannot repeat it.
    continuation = request.model_copy(update={"work_item_id": primary, "execution_id": "second"})
    with pytest.raises(UncertainOperation, match="unresolved"):
        workflow.advance_work_item(continuation)
    assert effects == [primary]
    assert journal.operations("second") == []

    journal.reconcile_operation(
        "first-execution", tool.name, '{"value":"synthetic"}', _receipt(),
    )
    fail_after_effect = False
    with activate_execution(journal, "second"):
        output = asyncio.run(tool.on_invoke_tool(None, '{"value":"synthetic"}'))
        assert json.loads(output) == _receipt()
    assert effects == [primary, primary]
    assert journal.operations("second")[0]["status"] == "verified"
