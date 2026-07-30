from __future__ import annotations

from typing import Any

import pytest

import keystone_agents.langgraph_workflow as langgraph_workflow
import keystone_agents.run as run_module
import keystone_agents.tools.gmail_tool as gmail_module
import keystone_agents.workflow_runner as workflow_runner
from keystone_agents.model_provider import ModelConfig
from keystone_agents.provider_read import current_provider_read_context
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.workflow_runner import PreparedWorkItemStep


@pytest.mark.parametrize("backend", ["work_item", "langgraph"])
def test_specialist_provider_read_context_reuses_client_and_snapshot(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_payload = [{"id": "message-1", "subject": "Bounded read"}]
    client_instances: list[Any] = []
    observed_contexts: list[Any] = []
    observed_snapshots: list[Any] = []

    class _FakeGmailTool:
        def __init__(self, *, live: bool) -> None:
            assert live is True
            client_instances.append(self)

        def list_recent_messages(self, **_kwargs: Any) -> list[dict[str, str]]:
            return provider_payload

    class _Agent:
        name = "gmail_triage"
        model = "gpt-test"
        instructions = "Return the typed result."
        tools: list[Any] = []
        model_settings = None
        output_type = ChiefOfStaffResult

    def fake_sdk_run(*_args: Any, **_kwargs: Any):
        context = current_provider_read_context()
        assert context is not None
        assert context.plan.provider == "gmail"
        observed_contexts.append(context)

        first = gmail_module.list_recent_messages(max_results=1)
        assert first is provider_payload
        first_snapshot = context.snapshot("gmail_list_recent_messages:1")
        second_snapshot = context.snapshot("gmail_list_recent_messages:1")
        assert first_snapshot is not None
        assert second_snapshot is first_snapshot
        assert first_snapshot.payload is provider_payload
        observed_snapshots.append(first_snapshot)

        same_client = context.service(
            "gmail.live_read_tool",
            lambda: pytest.fail("request-scoped Gmail client was rebuilt"),
        )
        assert same_client is client_instances[0]
        return (
            {"provider_payload": provider_payload},
            ChiefOfStaffResult(mode="llm", summary="Provider read complete.", audit_notes=[]),
        )

    def fake_specialist(
        work_item: WorkItem,
        *,
        request: WorkflowRunRequest,
        store: Any,
    ) -> WorkflowRunResult:
        del store
        typed_result = run_typed_sdk_agent(
            agent=_Agent(),
            typed_input={"request": request.request_text},
            output_type=ChiefOfStaffResult,
            live=True,
            config=ModelConfig(
                provider="openai",
                model="gpt-test",
                api_key="test-key",
            ),
        )
        completed = work_item.model_copy(update={"status": WorkItemStatus.DONE})
        return WorkflowRunResult(
            work_item=completed,
            route=WorkItemRoute.GMAIL_TRIAGE,
            status=WorkItemStatus.DONE,
            advanced=True,
            human_summary=typed_result.output.summary,
        )

    monkeypatch.setattr(gmail_module, "GmailTool", _FakeGmailTool)
    monkeypatch.setattr(run_module, "run_typed_sdk_sync", fake_sdk_run)
    monkeypatch.setattr(
        run_module,
        "enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )
    monkeypatch.setattr(workflow_runner, "_advance_gmail_triage", fake_specialist)

    request = WorkflowRunRequest(
        request_text="Read one bounded Gmail message.",
        live_sdk=True,
        save=False,
    )
    work_item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Bounded Gmail read",
        request_text=request.request_text,
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Gmail", object_type="mailbox"),
    )
    prepared = PreparedWorkItemStep(
        request=request,
        work_item=work_item,
        route=WorkItemRoute.GMAIL_TRIAGE,
        input_text=request.request_text,
        context_pack={},
    )

    if backend == "work_item":
        result = workflow_runner.run_prepared_work_item_specialist(prepared)
    else:
        state = langgraph_workflow._run_gmail_triage_node(
            {
                "prepared_step": {
                    "request": request.model_dump(mode="json"),
                    "work_item": work_item.model_dump(mode="json"),
                    "route": WorkItemRoute.GMAIL_TRIAGE.value,
                    "input_text": request.request_text,
                    "context_pack": {},
                },
                "node_path": ["prepare_work_item"],
            }
        )
        result = WorkflowRunResult.model_validate(state["result"])
        assert state["node_path"] == ["prepare_work_item", "run_gmail_triage"]

    assert result.human_summary == "Provider read complete."
    assert len(observed_contexts) == 1
    assert len(observed_snapshots) == 1
    assert len(client_instances) == 1
    assert current_provider_read_context() is None
