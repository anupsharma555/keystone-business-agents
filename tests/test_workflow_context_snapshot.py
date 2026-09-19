from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone_agents import langgraph_workflow as graph_runtime
from keystone_agents import workflow_runner
from keystone_agents.orchestration.checkpoints import (
    inspect_graph_execution,
    resume_graph_execution,
    run_durable_direct,
)
from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    execution_database_path,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItem, WorkItemKind
from keystone_agents.storage.sqlite_store import SQLiteStore


def source_bundle(tmp_path):
    fixture = Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
    payload = json.loads(fixture.read_text())
    path = tmp_path / "accepted-context.json"
    path.write_text(json.dumps(payload))
    return path, payload


def change_context_file(path, payload, change):
    if change == "delete":
        path.unlink()
    else:
        payload = json.loads(json.dumps(payload))
        payload["sources"][0]["source_id"] = "fixture:changed-after-checkpoint"
        payload["sources"][0]["supported_claim"] = "Changed evidence must not enter this execution."
        payload["facts"][0]["source_ids"] = ["fixture:changed-after-checkpoint"]
        path.write_text(json.dumps(payload))


@pytest.mark.parametrize("change", ["replace", "delete"])
def test_native_manager_resume_uses_accepted_sources_after_context_file_changes(
    tmp_path, monkeypatch, change
):
    pytest.importorskip("langgraph.graph", reason="This proof requires the compiled native graph.")
    pytest.importorskip(
        "langgraph.checkpoint.sqlite",
        reason="Native SQLite checkpoints require the orchestration extra.",
    )
    path, original = source_bundle(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    execution_id = "synthetic-context-recovery"
    request = WorkflowRunRequest(
        request_text=(
            "Gmail triage the supplied inbound request, research Northstar Behavioral "
            "Analytics using only the provided source bundle, and prepare a draft-only "
            "reply for review. Do not use live search or write externally."
        ),
        execution_id=execution_id,
        context_file_path=str(path),
        database_url=database_url,
        manual_request_plan={
            "source": "test",
            "requested_agent": "orchestrator",
            "target_agent": "gmail_triage",
            "intent": "gmail_triage",
            "primary_target": "Northstar Behavioral Analytics",
            "target_type": "gmail_thread",
            "task_objective": "gmail_triage",
        },
    )
    prepare = graph_runtime._prepare_work_item_node

    def fail_next_prepare(state):
        if state["request"]["request_text"] == "continue":
            raise RuntimeError("synthetic manager boundary failure")
        return prepare(state)

    monkeypatch.setattr(graph_runtime, "_prepare_work_item_node", fail_next_prepare)
    with pytest.raises(RuntimeError, match="manager boundary"):
        graph_runtime.run_work_item_langgraph(
            request, manager_loop=True, max_manager_steps=3, enable_interrupts=False
        )
    history = inspect_graph_execution(execution_id, database_url=database_url)
    assert history["history"][-1]["next_nodes"] == ["prepare_work_item"]
    store = ExecutionStore(execution_database_path(database_url))
    work_item_id = store.get(execution_id)["work_item_id"]
    assert work_item_id
    change_context_file(path, original, change)
    monkeypatch.setattr(graph_runtime, "_prepare_work_item_node", prepare)
    specialist_inputs = []
    research = graph_runtime._run_business_research_node

    def capture_research(state):
        prepared = graph_runtime._prepared_step_from_state(state)
        specialist_inputs.append(
            {source["source_id"] for source in prepared.context_pack["source_refs"]}
        )
        return research(state)

    monkeypatch.setattr(graph_runtime, "_run_business_research_node", capture_research)
    outcome = resume_graph_execution(execution_id, database_url=database_url)
    assert outcome.result.work_item.id == work_item_id
    assert specialist_inputs
    assert all("fixture:graph-source:company-brief" in sources for sources in specialist_inputs)
    assert all("fixture:changed-after-checkpoint" not in sources for sources in specialist_inputs)
    assert "fixture:changed-after-checkpoint" not in {
        s.source_id for s in outcome.result.work_item.sources
    }


@pytest.mark.parametrize("identity", ["execution_id", "origin_event_id"])
@pytest.mark.parametrize("change", ["replace", "delete"])
def test_direct_retry_reuses_accepted_context_by_identity(tmp_path, identity, change):
    path, original = source_bundle(tmp_path)
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    business = SQLiteStore(database_url)
    work_item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic source review")
    business.save_work_item(work_item)
    request = WorkflowRunRequest(
        request_text="Research Northstar Behavioral Analytics using only the supplied material.",
        work_item_id=work_item.id,
        context_file_path=str(path),
        database_url=database_url,
        **{identity: "synthetic-direct-context"},
    )
    accepted = []

    def runner(current):
        accepted.append(workflow_runner._load_external_context(current))
        result = workflow_runner.advance_work_item(current)
        if len(accepted) == 1:
            raise RuntimeError("synthetic direct boundary failure")
        return result

    with pytest.raises(RuntimeError, match="direct boundary"):
        run_durable_direct(request, runner)
    change_context_file(path, original, change)
    result = run_durable_direct(request, runner)
    assert result.work_item.id == work_item.id
    assert accepted == [original, original]
    assert len(ExecutionStore(execution_database_path(database_url)).list_executions()) == 1


def test_existing_execution_rejects_changed_request_even_if_context_file_was_deleted(tmp_path):
    path, _ = source_bundle(tmp_path)
    request = WorkflowRunRequest(
        request_text="Original accepted request",
        context_file_path=str(path),
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
        execution_id="synthetic-request-drift",
    )

    def fail(_request):
        raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError, match="synthetic failure"):
        run_durable_direct(request, fail)
    path.unlink()
    with pytest.raises(ExecutionConflict):
        run_durable_direct(request.model_copy(update={"request_text": "Different operation"}), fail)


@pytest.mark.parametrize(
    "file_payload", [{}, {"shared": "file", "slack_query_prompt": {"source": "file"}}]
)
def test_snapshot_preserves_existing_merge_precedence_and_detaches_payload(tmp_path, file_payload):
    from keystone_agents.runtime.context_snapshot import freeze_workflow_context

    path = tmp_path / "context.json"
    path.write_text(json.dumps(file_payload))
    external = {
        "shared": "external",
        "external_only": ["accepted"],
        "slack_query_prompt": {"source": "external"},
    }
    request = WorkflowRunRequest(
        context_file_path=str(path),
        external_context=external,
        slack_query_prompt={"source": "request"},
    )
    expected = {"slack_query_prompt": request.slack_query_prompt, **external, **file_payload}
    expected = json.loads(json.dumps(expected))
    frozen = freeze_workflow_context(request)
    path.unlink()
    external["external_only"].append("mutated after acceptance")
    loaded = workflow_runner._load_external_context(frozen)
    assert loaded == expected
    loaded["external_only"].append("downstream mutation")
    assert workflow_runner._load_external_context(frozen) == expected


def test_slack_scope_session_and_metadata_survive_deleted_context_file(tmp_path):
    from keystone_agents.runtime.context_snapshot import freeze_workflow_context
    from keystone_agents.slack_actions import (
        RUN_AGENT_MESSAGE_CALLBACK_ID,
        build_selected_message_context,
    )

    payload = build_selected_message_context(
        {
            "type": "message_action",
            "callback_id": RUN_AGENT_MESSAGE_CALLBACK_ID,
            "team": {"id": "T_SYNTHETIC"},
            "channel": {"id": "C_SYNTHETIC"},
            "message": {
                "ts": "1789060000.000001",
                "user": "U_SYNTHETIC",
                "text": "Accepted evidence",
            },
        }
    ).model_dump(mode="json", by_alias=True)
    path = tmp_path / "slack-context.json"
    path.write_text(json.dumps(payload))
    work_item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic Slack review")
    request = WorkflowRunRequest(context_file_path=str(path), sdk_session_enabled=True)
    original_spec = workflow_runner._sdk_session_spec_for_work_item(request, work_item)
    frozen = freeze_workflow_context(request)
    path.unlink()
    assert workflow_runner._request_has_slack_context(frozen)
    spec = workflow_runner._sdk_session_spec_for_work_item(frozen, work_item)
    assert spec.scope == original_spec.scope == "slack"
    assert spec.session_id == original_spec.session_id
    assert spec.history_limit == original_spec.history_limit == 6
    applied = workflow_runner._apply_external_context(
        work_item,
        workflow_runner._load_external_context(frozen),
        context_file_path=frozen.context_file_path,
    )
    assert applied.target.metadata["slack_context"]["team_id"] == "T_SYNTHETIC"
    assert (
        applied.target.metadata["slack_context"]["selected_message"]["text"] == "Accepted evidence"
    )


@pytest.mark.parametrize("source", ["file_secret", "runtime_client"])
def test_noncheckpointable_context_is_rejected_before_execution_persistence(tmp_path, source):
    request = WorkflowRunRequest(
        request_text="Review supplied context",
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
    )
    if source == "file_secret":
        path = tmp_path / "context.json"
        path.write_text(json.dumps({"access_token": "synthetic-sensitive-placeholder"}))
        request = request.model_copy(update={"context_file_path": str(path)})
    else:
        request = request.model_copy(update={"external_context": {"client": object()}})
    with pytest.raises(ExecutionConflict, match="redaction|only JSON"):
        run_durable_direct(request, lambda _: pytest.fail("runner must not execute"))
    store = ExecutionStore(execution_database_path(request.database_url))
    assert store.list_executions() == []
    assert b"synthetic-sensitive-placeholder" not in store.path.read_bytes()
