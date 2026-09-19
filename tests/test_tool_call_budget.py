from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.journal import (
    instrument_agent_tools,
    reset_tool_receipt_journal,
)
from keystone_agents.runtime.tool_call_budget import (
    ToolCallBudgetContract,
    ToolCallBudgetExceededError,
    ToolCallBudgetLedger,
    ToolCallLimit,
)
from keystone_agents.runtime.tool_execution import SDKToolExecutionRecord


def test_tool_call_budget_admission_is_atomic_across_parallel_calls() -> None:
    ledger = ToolCallBudgetLedger(
        ToolCallBudgetContract(
            limits=(ToolCallLimit("search_web", 4),),
            max_total_calls=4,
            stage="research_selection",
        )
    )

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(lambda _index: ledger.admit("search_web"), range(20)))

    assert sum(outcome.admitted for outcome in outcomes) == 4
    snapshot = ledger.snapshot()
    assert snapshot["total_consumed"] == 4
    assert snapshot["total_blocked"] == 16
    assert snapshot["consumed"]["search_web"] == 4
    assert snapshot["blocked"]["search_web"] == 16


def test_instrumented_read_is_blocked_before_provider_after_budget() -> None:
    reset_tool_receipt_journal()
    provider_calls = 0

    async def read_provider(_context: object, _tool_input: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return json.dumps({"status": "read", "provider_read_performed": True})

    tool = SimpleNamespace(
        name="search_web",
        on_invoke_tool=read_provider,
        is_enabled=True,
    )
    ledger = ToolCallBudgetLedger(
        ToolCallBudgetContract(
            limits=(ToolCallLimit("search_web", 1),),
            stage="research_selection",
        )
    )
    instrument_agent_tools(SimpleNamespace(tools=[tool]), tool_call_budget=ledger)

    first = asyncio.run(tool.on_invoke_tool(None, "{}"))
    second = json.loads(asyncio.run(tool.on_invoke_tool(None, "{}")))

    assert json.loads(first)["status"] == "read"
    assert provider_calls == 1
    assert second["status"] == "blocked"
    assert second["recoverable"] is True
    assert second["provider_action_performed"] is False


def test_instrumented_mutation_overflow_fails_closed_without_retry() -> None:
    reset_tool_receipt_journal()
    provider_writes = 0

    async def create_event(_context: object, _tool_input: str) -> str:
        nonlocal provider_writes
        provider_writes += 1
        return json.dumps({"status": "created", "provider_write": True})

    tool = SimpleNamespace(
        name="create_calendar_event",
        on_invoke_tool=create_event,
        is_enabled=True,
    )
    ledger = ToolCallBudgetLedger(
        ToolCallBudgetContract(max_total_calls=1, stage="calendar_mutation")
    )
    instrument_agent_tools(SimpleNamespace(tools=[tool]), tool_call_budget=ledger)

    asyncio.run(tool.on_invoke_tool(None, "{}"))
    blocked = json.loads(asyncio.run(tool.on_invoke_tool(None, "{}")))

    assert provider_writes == 1
    assert blocked["status"] == "blocked"
    assert blocked["reason_code"] == "total_tool_call_budget_exhausted"
    assert blocked["recoverable"] is False
    assert blocked["provider_action_performed"] is False


def test_reinstrumented_reused_tool_receives_a_fresh_request_budget() -> None:
    reset_tool_receipt_journal()
    provider_calls = 0

    async def read_provider(_context: object, _tool_input: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        return json.dumps({"status": "read", "provider_read_performed": True})

    tool = SimpleNamespace(
        name="search_web",
        on_invoke_tool=read_provider,
        is_enabled=True,
    )
    agent = SimpleNamespace(tools=[tool])
    first_budget = ToolCallBudgetLedger(
        ToolCallBudgetContract(max_total_calls=1, stage="first_request")
    )
    instrument_agent_tools(agent, tool_call_budget=first_budget)
    asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert json.loads(asyncio.run(tool.on_invoke_tool(None, "{}")))["status"] == (
        "blocked"
    )

    second_budget = ToolCallBudgetLedger(
        ToolCallBudgetContract(max_total_calls=1, stage="second_request")
    )
    instrument_agent_tools(agent, tool_call_budget=second_budget)
    second_request_first_call = json.loads(
        asyncio.run(tool.on_invoke_tool(None, "{}"))
    )

    assert second_request_first_call["status"] == "read"
    assert provider_calls == 2
    assert second_budget.snapshot()["total_consumed"] == 1


def test_hosted_tool_calls_are_counted_and_overage_fails_post_response() -> None:
    budget = ToolCallBudgetLedger(
        ToolCallBudgetContract(
            limits=(ToolCallLimit("file_search", 1),),
            max_total_calls=1,
            stage="rag_vector_store_retrieval",
        )
    )
    first = SDKToolExecutionRecord(
        call_id="fs-1",
        tool_name="file_search",
        call_index=1,
        output_observed=True,
        succeeded=True,
        status="completed",
        self_contained_hosted=True,
    )
    second = SDKToolExecutionRecord(
        call_id="fs-2",
        tool_name="file_search",
        call_index=2,
        output_observed=True,
        succeeded=True,
        status="completed",
        self_contained_hosted=True,
    )

    budget.observe_hosted_calls((first,))
    budget.observe_hosted_calls((first,))
    snapshot = budget.snapshot()

    assert snapshot["total_consumed"] == 1
    assert snapshot["consumed"]["file_search"] == 1
    assert snapshot["hosted_observed_total"] == 1
    assert snapshot["hosted_provider_action_prevented_on_overage"] is False

    with pytest.raises(ToolCallBudgetExceededError, match="consumed 2 calls"):
        budget.observe_hosted_calls((second,))
