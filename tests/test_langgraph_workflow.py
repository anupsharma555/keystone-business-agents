from __future__ import annotations

from pathlib import Path

import pytest

from keystone_agents.langgraph_workflow import (
    LangGraphUnavailableError,
    build_work_item_langgraph,
    langgraph_available,
    langgraph_functionality_improvements,
    next_langgraph_node_for_result,
    run_work_item_langgraph,
    work_item_graph_thread_id,
    work_item_langgraph_enabled,
)
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


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'langgraph_workflow.db'}"


def test_optional_langgraph_workflow_advances_existing_work_item_runner(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)

    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            database_url=database_url,
            save=True,
        )
    )

    store = SQLiteStore(database_url)
    events = store.list_work_item_events(outcome.result.work_item.id)

    assert outcome.result.advanced is True
    assert outcome.result.route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert outcome.result.context_pack is not None
    assert outcome.result.context_pack["pack_type"] == "research"
    assert outcome.graph_runtime in {"langgraph", "dependency_free_fallback"}
    assert outcome.node_path == ["advance_work_item"]
    assert "advance_work_item" in outcome.node_path
    assert any(event.event_type == "langgraph_orchestration" for event in events)
    assert any("checkpoint seam" in item for item in outcome.improvements)


def test_langgraph_checkpoint_routing_for_approval_gated_result() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.OUTREACH, title="Draft outreach"),
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=WorkItemStatus.NEEDS_APPROVAL,
        advanced=True,
        next_action=WorkItemNextAction(
            action="review_outreach_draft",
            agent=WorkItemRoute.OUTREACH_COMPOSER,
            description="Review the draft approval item before any external use.",
            requires_approval=True,
        ),
    )

    assert next_langgraph_node_for_result(result) == "approval_checkpoint"


def test_langgraph_runtime_can_be_required_only_when_installed() -> None:
    if langgraph_available():
        assert build_work_item_langgraph() is not None
    else:
        with pytest.raises(LangGraphUnavailableError):
            build_work_item_langgraph()


def test_langgraph_improvements_preserve_sdk_first_contract() -> None:
    improvements = " ".join(langgraph_functionality_improvements())

    assert "WorkItem" in improvements
    assert "SQLite" in improvements
    assert "SDK specialist agents" in improvements
    assert "Python safety gates" in improvements


def test_langgraph_env_aliases_enable_optional_work_item_runtime() -> None:
    assert work_item_langgraph_enabled({}) is False
    assert work_item_langgraph_enabled({"KEYSTONE_WORKITEM_LANGGRAPH": "true"}) is True
    assert work_item_langgraph_enabled({"KNI_BUSINESS_AGENTS_LANGGRAPH": "1"}) is True
    assert (
        work_item_langgraph_enabled(
            {
                "KNI_BUSINESS_AGENTS_LANGGRAPH": "false",
                "KEYSTONE_WORKITEM_LANGGRAPH": "true",
            }
        )
        is True
    )
    assert work_item_langgraph_enabled({"KNI_BUSINESS_AGENTS_LANGGRAPH": "off"}) is False


def test_langgraph_work_item_thread_id_is_stable() -> None:
    assert work_item_graph_thread_id("wi_123") == "work-item:wi_123"
    assert work_item_graph_thread_id("") == ""
