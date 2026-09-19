"""Adversarial, provider-free checks at real V2 persistence boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace

import pytest

import keystone_agents.langgraph_workflow as graph_runtime
import keystone_agents.orchestration.checkpoints as checkpoints
from keystone_agents.receipts.journal import (
    instrument_agent_tools,
    record_provider_observation,
)
from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    UncertainOperation,
    activate_execution,
    execution_database_path,
)
from keystone_agents.runtime.work_item_lock import work_item_execution_lock
from keystone_agents.schemas.approval import ApprovalQueueStatus, ApprovalState
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.signal_lifecycle_tools import (
    advance_signal_lifecycle_checkpoint_impl,
    prepare_signal_lifecycle_checkpoint_impl,
)
from keystone_agents.work_items import (
    apply_slack_approval_to_work_item_gate,
    approve_artifact_context,
)


def _receipt(*, verified: bool = True) -> dict:
    return {
        "status": "success" if verified else "observed",
        "provider": "airtable",
        "operation": "create_record",
        "record_id": "rec_synthetic_v2",
        "provider_write": True,
        "verification": {"passed": verified},
    }


def test_equivalent_sdk_json_arguments_reuse_one_verified_mutation(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    execution_id = store.begin({"request": "synthetic"})["id"]
    calls = []

    async def create(_context, arguments):
        calls.append(json.loads(arguments))
        return json.dumps(_receipt())

    tool = SimpleNamespace(name="airtable_create_record", on_invoke_tool=create)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    with activate_execution(store, execution_id):
        first = asyncio.run(tool.on_invoke_tool(None, '{"table":"synthetic","value":1}'))
        second = asyncio.run(tool.on_invoke_tool(None, '{ "value": 1, "table": "synthetic" }'))

    assert len(calls) == 1
    assert len(store.operations(execution_id)) == 1
    assert json.loads(first) == json.loads(second)


def test_distinct_business_database_filenames_do_not_share_execution_store(tmp_path) -> None:
    first = execution_database_path(f"sqlite:///{tmp_path / 'business.db'}")
    second = execution_database_path(f"sqlite:///{tmp_path / 'business.sqlite3'}")

    assert first != second


def test_equivalent_airtable_fields_json_reuses_one_provider_operation(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    execution_id = store.begin({"request": "synthetic"})["id"]
    calls = []

    async def create(_context, arguments):
        calls.append(json.loads(json.loads(arguments)["fields_json"]))
        return json.dumps(_receipt())

    tool = SimpleNamespace(name="airtable_write_record", on_invoke_tool=create)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    first = {
        "fields_json": '{"Item":"KBA_TEST_RECORD synthetic","Description":"example"}',
        "table": "Business Expenses",
        "operation": "create",
    }
    second = {
        **first,
        "fields_json": '{ "Description": "example", "Item": "KBA_TEST_RECORD synthetic" }',
    }
    with activate_execution(store, execution_id):
        asyncio.run(tool.on_invoke_tool(None, json.dumps(first)))
        asyncio.run(tool.on_invoke_tool(None, json.dumps(second)))

    assert calls == [{"Item": "KBA_TEST_RECORD synthetic", "Description": "example"}]
    assert len(store.operations(execution_id)) == 1


def test_literal_document_text_remains_distinct_from_json_argument_structure(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    execution_id = store.begin({"request": "synthetic"})["id"]

    assert (
        store.before_operation(
            execution_id,
            "google_doc_update",
            {"body": '{"value":1}'},
        )
        is None
    )
    receipt = _receipt()
    receipt.update(provider="google_workspace", operation="update_document")
    receipt["document_id"] = receipt.pop("record_id")
    store.observe_operation(execution_id, "google_doc_update", {"body": '{"value":1}'}, receipt)
    assert (
        store.before_operation(
            execution_id,
            "google_doc_update",
            {"body": '{ "value": 1 }'},
        )
        is None
    )
    assert len(store.operations(execution_id)) == 2


def test_copied_context_cannot_reuse_expired_work_item_ownership(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'business.db'}"
    with work_item_execution_lock(database, "wi_synthetic", owner="first-execution"):
        stale = copy_context()

    def delayed_callback():
        with work_item_execution_lock(database, "wi_synthetic", owner="first-execution"):
            pytest.fail("expired context bypassed the current WorkItem owner")

    with work_item_execution_lock(database, "wi_synthetic", owner="second-execution"):
        with pytest.raises(ExecutionConflict, match="expired"):
            stale.run(delayed_callback)
    # The stale context remains expired even after the next owner releases.
    with pytest.raises(ExecutionConflict, match="expired"):
        stale.run(delayed_callback)


def test_composite_readback_crash_retains_object_without_reissuing_create(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    execution_id = store.begin({"request": "synthetic"})["id"]
    calls = []

    async def create_then_read(_context, _arguments):
        calls.append("create")
        record_provider_observation(_receipt(verified=False))
        raise ConnectionError("synthetic read-back failure")

    tool = SimpleNamespace(name="airtable_create_record", on_invoke_tool=create_then_read)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    arguments = '{"value":1}'
    with activate_execution(store, execution_id):
        with pytest.raises(ConnectionError):
            asyncio.run(tool.on_invoke_tool(None, arguments))
    reopened = ExecutionStore(store.path)
    assert reopened.operations(execution_id)[0]["status"] == "observed"
    with activate_execution(reopened, execution_id):
        with pytest.raises(UncertainOperation):
            asyncio.run(tool.on_invoke_tool(None, arguments))
        reopened.reconcile_operation(execution_id, tool.name, arguments, _receipt())
        result = json.loads(asyncio.run(tool.on_invoke_tool(None, arguments)))
    assert result["record_id"] == "rec_synthetic_v2"
    assert calls == ["create"]


@pytest.mark.parametrize("backend", ["direct", "graph"])
def test_stale_prelock_start_cannot_repeat_a_completed_execution(
    tmp_path, monkeypatch, backend
) -> None:
    if backend == "graph":
        pytest.importorskip("langgraph.checkpoint.sqlite")
    first_at_lock = threading.Event()
    second_at_lock = threading.Event()
    release_second = threading.Event()
    actual_lock = checkpoints.execution_lock
    errors = []
    results = []
    calls = []

    @contextmanager
    def ordered_lock(path):
        if threading.current_thread().name == "first-owner":
            first_at_lock.set()
            assert second_at_lock.wait(15)
        else:
            second_at_lock.set()
            assert release_second.wait(15)
        with actual_lock(path):
            yield

    monkeypatch.setattr(checkpoints, "execution_lock", ordered_lock)
    request = WorkflowRunRequest(
        request_text="research Example Analytics",
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
        execution_id="synthetic-concurrent-start",
        live_sdk=False,
        live_search=False,
    )
    original_specialist = graph_runtime._run_business_research_node

    def specialist(state):
        calls.append("research")
        return original_specialist(state)

    monkeypatch.setattr(graph_runtime, "_run_business_research_node", specialist)

    def direct_runner(_request):
        calls.append("direct")
        item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic result")
        return WorkflowRunResult(
            work_item=item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
        )

    def run():
        try:
            results.append(
                checkpoints.run_durable_direct(request, direct_runner)
                if backend == "direct"
                else graph_runtime.run_work_item_langgraph(request)
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run, name="first-owner")
    second = threading.Thread(target=run, name="second-owner")
    first.start()
    assert first_at_lock.wait(15)
    second.start()
    first.join(20)
    release_second.set()
    second.join(20)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("holder", ["direct", "graph"])
def test_distinct_graph_and_direct_executions_cannot_advance_one_work_item(
    tmp_path,
    monkeypatch,
    holder,
) -> None:
    database = f"sqlite:///{tmp_path / 'shared-business.db'}"
    store = SQLiteStore(database)
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic shared WorkItem",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(item)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    errors = []
    original_specialist = graph_runtime._run_business_research_node

    def wait_if_holder(backend):
        calls.append(backend)
        if holder == backend:
            entered.set()
            assert release.wait(15)

    def graph_specialist(state):
        wait_if_holder("graph")
        return original_specialist(state)

    monkeypatch.setattr(graph_runtime, "_run_business_research_node", graph_specialist)

    def direct_runner(_request):
        current = store.get_work_item(item.id)
        wait_if_holder("direct")
        store.save_work_item(current.model_copy(update={"title": "Updated synthetic WorkItem"}))
        return WorkflowRunResult(
            work_item=current,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.DONE,
            advanced=True,
        )

    def execute(backend):
        request = WorkflowRunRequest(
            request_text="research Example Analytics",
            database_url=database,
            work_item_id=item.id,
            execution_id=f"synthetic-distinct-{backend}",
            live_sdk=False,
            live_search=False,
        )
        if backend == "direct":
            return checkpoints.run_durable_direct(request, direct_runner)
        return graph_runtime.run_work_item_langgraph(request)

    def first():
        try:
            execute(holder)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(15)
    try:
        with pytest.raises(ExecutionConflict, match="WorkItem.*already"):
            execute("graph" if holder == "direct" else "direct")
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive()
    assert errors == []
    assert calls == [holder]
    # Losing admission did not start work or consume the second execution: it
    # remains retryable after the first owner releases the shared WorkItem.
    other = "graph" if holder == "direct" else "direct"
    execute(other)
    assert calls == [holder, other]


def _crash_before_finalization(tmp_path, monkeypatch):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    request = WorkflowRunRequest(
        request_text="research Example Analytics",
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
        execution_id="synthetic-paused",
        live_sdk=False,
        live_search=False,
    )
    original = graph_runtime._finalize_step_node

    def crash(_state):
        raise RuntimeError("synthetic pause")

    monkeypatch.setattr(graph_runtime, "_finalize_step_node", crash)
    with pytest.raises(RuntimeError, match="synthetic pause"):
        graph_runtime.run_work_item_langgraph(request)
    monkeypatch.setattr(graph_runtime, "_finalize_step_node", original)
    return request


def test_archived_work_item_revokes_paused_graph_execution(tmp_path, monkeypatch) -> None:
    request = _crash_before_finalization(tmp_path, monkeypatch)
    execution = ExecutionStore(execution_database_path(request.database_url)).get(
        request.execution_id
    )
    store = SQLiteStore(request.database_url)
    item = store.get_work_item(execution["work_item_id"])
    store.save_work_item(item.model_copy(update={"status": WorkItemStatus.ARCHIVED}))

    with pytest.raises(ExecutionConflict, match="(?i)archived|revok|changed"):
        checkpoints.resume_graph_execution(request.execution_id, database_url=request.database_url)
    assert store.get_work_item(item.id).status == WorkItemStatus.ARCHIVED


def test_two_concurrent_resumes_cannot_execute_the_pending_node_twice(
    tmp_path, monkeypatch
) -> None:
    request = _crash_before_finalization(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []
    errors = []
    outcomes = []
    finalize = graph_runtime._finalize_step_node

    def pending_node(state):
        calls.append("finalize")
        entered.set()
        assert release.wait(15)
        return finalize(state)

    monkeypatch.setattr(graph_runtime, "_finalize_step_node", pending_node)

    def first_resume():
        try:
            outcomes.append(
                checkpoints.resume_graph_execution(
                    request.execution_id,
                    database_url=request.database_url,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=first_resume)
    worker.start()
    assert entered.wait(15)
    try:
        with pytest.raises(ExecutionConflict, match="already running"):
            checkpoints.resume_graph_execution(
                request.execution_id, database_url=request.database_url
            )
    finally:
        release.set()
        worker.join(20)
    assert not worker.is_alive()
    assert errors == []
    assert len(outcomes) == 1
    assert calls == ["finalize"]


def test_changed_runtime_dependency_blocks_checkpoint_resume(tmp_path, monkeypatch) -> None:
    fingerprint = checkpoints.current_runtime_fingerprint()
    monkeypatch.setattr(checkpoints, "current_runtime_fingerprint", lambda: fingerprint)
    request = _crash_before_finalization(tmp_path, monkeypatch)
    fingerprint = {
        **fingerprint,
        "runtime_sha256": "changed-runtime-revision",
        "dependency_sha256": "changed-dependency-revision",
        "package_versions": {**fingerprint.get("package_versions", {}), "langgraph": "99.0.0"},
    }

    with pytest.raises(ExecutionConflict, match="(?i)runtime|version|changed|migration"):
        checkpoints.resume_graph_execution(request.execution_id, database_url=request.database_url)


def test_resume_respects_another_execution_owning_the_same_work_item(tmp_path, monkeypatch) -> None:
    request = _crash_before_finalization(tmp_path, monkeypatch)
    execution = ExecutionStore(execution_database_path(request.database_url)).get(
        request.execution_id
    )
    with work_item_execution_lock(
        request.database_url,
        execution["work_item_id"],
        owner="different-execution",
    ):
        with pytest.raises(ExecutionConflict, match="WorkItem.*already"):
            checkpoints.resume_graph_execution(
                request.execution_id, database_url=request.database_url
            )
    assert (
        checkpoints.resume_graph_execution(
            request.execution_id,
            database_url=request.database_url,
        ).durable
        is True
    )


def test_signal_transition_shares_work_item_ownership_without_self_deadlock(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'signals.db'}"
    store = SQLiteStore(database)
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic signal",
        current_route=WorkItemRoute.RSS_CONTEXT_AGENT,
    )
    store.save_work_item(item)
    arguments = {
        "work_item_id": item.id,
        "source_kind": "rss",
        "trigger_ref": "synthetic-trigger",
        "dedupe_scope": "synthetic-scope",
        "candidate_items_json": '[{"feed_item_id":"synthetic","url":"https://example.com/item"}]',
        "database_url": database,
        "dry_run": False,
    }
    executions = ExecutionStore(execution_database_path(database))
    execution_id = executions.begin({"request": "synthetic"})["id"]
    with work_item_execution_lock(database, item.id, owner="different-execution"):
        # This is a genuinely different execution, not a nested signal stage
        # inheriting the outer advancement owner's context.
        with activate_execution(executions, execution_id):
            with pytest.raises(ExecutionConflict, match="WorkItem.*already"):
                prepare_signal_lifecycle_checkpoint_impl(**arguments)
    assert store.get_work_item(item.id).artifact_refs == []
    with activate_execution(executions, execution_id):
        with work_item_execution_lock(database, item.id):
            prepared = prepare_signal_lifecycle_checkpoint_impl(**arguments)
            result = advance_signal_lifecycle_checkpoint_impl(
                work_item_id=item.id,
                trigger_id=prepared["checkpoint"]["trigger_id"],
                completed_stage="context_retrieved",
                dry_run=False,
                database_url=database,
            )
    assert result["checkpoint"]["completed_stages"] == ["context_retrieved"]


def test_changed_runtime_dependency_blocks_failed_direct_replay(tmp_path, monkeypatch) -> None:
    fingerprint = checkpoints.current_runtime_fingerprint()
    monkeypatch.setattr(checkpoints, "current_runtime_fingerprint", lambda: fingerprint)
    request = WorkflowRunRequest(
        request_text="Read the synthetic source.",
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
        execution_id="synthetic-direct-runtime-change",
    )
    calls = []

    def failing_runner(_request):
        calls.append("attempt")
        raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        checkpoints.run_durable_direct(request, failing_runner)
    fingerprint = {**fingerprint, "runtime_sha256": "changed-runtime-revision"}
    with pytest.raises(ExecutionConflict, match="(?i)runtime|version|changed|migration"):
        checkpoints.run_durable_direct(request, failing_runner)
    assert calls == ["attempt"]


def test_inspection_does_not_initialize_graph_tables_for_direct_execution(tmp_path) -> None:
    pytest.importorskip("langgraph.checkpoint.sqlite")
    database = f"sqlite:///{tmp_path / 'business.db'}"
    store = ExecutionStore(execution_database_path(database))
    execution_id = store.begin({"request": "synthetic", "backend": "direct"})["id"]

    def tables():
        with store.connection() as connection:
            return {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    before = tables()
    try:
        checkpoints.inspect_graph_execution(execution_id, database_url=database)
    except ExecutionConflict:
        pass
    assert tables() == before


def test_native_approval_resume_requires_authoritative_approval_and_reuses_draft(tmp_path) -> None:
    pytest.importorskip("langgraph.checkpoint.sqlite")
    database = f"sqlite:///{tmp_path / 'business.db'}"
    research = graph_runtime.run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research Example Analytics",
            database_url=database,
        )
    ).result
    reference = research.artifact_refs[0]
    item = approve_artifact_context(
        research.work_item,
        reference.artifact_type,
        reference.artifact_id,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    ).model_copy(
        update={
            "next_action": WorkItemNextAction(
                action="draft_outreach",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
            )
        }
    )
    store = SQLiteStore(database)
    store.save_work_item(item)
    request = WorkflowRunRequest(
        request_text="continue",
        database_url=database,
        work_item_id=item.id,
        execution_id="synthetic-approval",
    )
    paused = graph_runtime.run_work_item_langgraph(
        request, enable_interrupts=True, manager_loop=True
    )
    assert paused.interrupted is True
    with pytest.raises(ExecutionConflict, match="approval|approved"):
        checkpoints.resume_graph_execution(
            request.execution_id, database_url=database, approved=True
        )
    gate = next(
        gate for gate in paused.result.work_item.approval_gates if gate.scope == "external_use"
    )
    drafts = [
        a.artifact_id
        for a in paused.result.work_item.artifact_refs
        if a.artifact_type == "outreach_draft"
    ]
    apply_slack_approval_to_work_item_gate(
        paused.result.work_item,
        gate.approval_id,
        ApprovalQueueStatus.APPROVED,
        actor="synthetic-operator",
        notes="Exact local approval.",
        store=store,
    )
    resumed = checkpoints.resume_graph_execution(
        request.execution_id,
        database_url=database,
        approved=True,
    )
    assert resumed.interrupted is False
    assert resumed.checkpoint_required is False
    assert resumed.result.status == WorkItemStatus.DONE
    assert [
        a.artifact_id
        for a in resumed.result.work_item.artifact_refs
        if a.artifact_type == "outreach_draft"
    ] == drafts


def test_fresh_process_recovers_native_checkpoint_without_repeating_specialist(tmp_path) -> None:
    pytest.importorskip("langgraph.checkpoint.sqlite")
    code = r"""
import json, os, sys
from pathlib import Path
import keystone_agents.langgraph_workflow as graph
import keystone_agents.orchestration.checkpoints as checkpoints
from keystone_agents.schemas.work_item import WorkflowRunRequest
checkpoints.current_runtime_fingerprint = lambda: {"source_sha256": "synthetic-stable-revision"}
root, mode = Path(sys.argv[1]), sys.argv[2]
database = "sqlite:///" + str(root / "business.db")
original = graph._run_business_research_node
def specialist(state):
    with (root / "calls.txt").open("a") as handle: handle.write("research\n")
    return original(state)
graph._run_business_research_node = specialist
if mode == "crash":
    graph._finalize_step_node = lambda state: os._exit(13)
    graph.run_work_item_langgraph(WorkflowRunRequest(
        request_text="research Example Analytics", database_url=database,
        execution_id="synthetic-process-recovery", live_sdk=False, live_search=False,
    ))
else:
    result = checkpoints.resume_graph_execution("synthetic-process-recovery", database_url=database)
    print(json.dumps({"durable": result.durable, "execution_id": result.execution_id}))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))
    }
    environment.update(
        {
            "KEYSTONE_DRY_RUN": "true",
            "KEYSTONE_LIVE_MODE": "false",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    first = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), "crash"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert first.returncode == 13, first.stderr
    second = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), "resume"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout)["durable"] is True
    assert (tmp_path / "calls.txt").read_text().splitlines() == ["research"]
