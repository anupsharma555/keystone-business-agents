"""Offline request-scope and identity gates for lazy channel-context tools."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from keystone_agents.agents.orchestrator import build_orchestrator_agent
from keystone_agents.tools.slack_channel_context_tools import (
    SlackChannelContextCapability,
    channel_context_capability_from_state,
)


def descriptor():
    return {"schema_version": 1, "channel_id": "CSYNTHETIC",
            "request_ts": "1800000000.000001", "oldest_ts": "1799395200.000001"}


def provider_message(**updates):
    return {"candidate_id": "message:synthetic", "channel_id": "CSYNTHETIC",
            "message_ts": "1799990000.000001", "thread_ts": "1799990000.000001",
            "text": "A digest contains three source links.",
            "source_links": [{"url": f"https://example.org/article/{index}", "ordinal": index}
                             for index in range(1, 4)], **updates}


def capability_with_backend(message=None):
    calls = []

    def backend(payload):
        calls.append(payload)
        return {"ok": True, "messages": [message or provider_message()], "coverage": {
            "pages": 1, "scanned_count": 10, "returned_count": 1,
            "truncated": True, "next_cursor": "bounded-next-page", "errors": [],
        }}

    return SlackChannelContextCapability(descriptor(), backend=backend), calls


def test_builder_has_only_narrow_context_tools_in_route_only_mode():
    capability, calls = capability_with_backend()
    agent = build_orchestrator_agent(route_only=True, channel_context_capability=capability)
    assert {tool.name for tool in agent.tools} == {
        "query_slack_channel_context", "read_slack_channel_context",
    }
    assert not agent.handoffs
    assert not calls
    assert build_orchestrator_agent(route_only=True).tools == []
    for tool in agent.tools:
        assert "channel_id" not in tool.params_json_schema["properties"]


def test_scope_is_copied_and_exact_read_uses_previously_returned_identity():
    scope = descriptor()
    calls = []

    def backend(payload):
        calls.append(deepcopy(payload))
        return {"ok": True, "messages": [provider_message()], "coverage": {"truncated": False}}

    capability = SlackChannelContextCapability(scope, backend=backend)
    scope["channel_id"] = "COTHER"
    capability.descriptor["channel_id"] = "COTHER"
    assert not capability.read("message:guessed")["ok"]
    assert not calls
    queried = capability.query(query="digest")
    read = capability.read(queried["messages"][0]["candidate_id"])
    assert read["ok"]
    assert len(calls) == 2
    assert all(item["descriptor"]["channel_id"] == "CSYNTHETIC" for item in calls)
    assert calls[1]["arguments"] == {
        "message_ts": "1799990000.000001", "thread_ts": "1799990000.000001",
    }
    assert read["messages"][0]["source_links"][2] == {
        "url": "https://example.org/article/3", "ordinal": 3,
    }


def test_private_channel_scope_keeps_team_binding_through_query_and_read():
    scope = {**descriptor(), "channel_id": "G111PRIVATE", "team_id": "T111TEST"}
    calls = []

    def backend(payload):
        calls.append(deepcopy(payload))
        return {"ok": True, "messages": [provider_message(channel_id="G111PRIVATE")],
                "coverage": {"pages": 1}}

    capability = SlackChannelContextCapability(scope, backend=backend)
    scope["team_id"] = "T222OTHER"
    result = capability.query(query="digest")
    assert result["ok"]
    assert capability.read(result["messages"][0]["candidate_id"])["ok"]
    assert len(calls) == 2
    assert all(call["descriptor"]["team_id"] == "T111TEST" for call in calls)
    assert all(call["descriptor"]["channel_id"] == "G111PRIVATE" for call in calls)


@pytest.mark.parametrize("arguments", [
    {}, {"query": "digest", "from_ts": "1799395199"},
    {"query": "digest", "to_ts": "1800000001"},
    {"query": "digest", "thread_ts": "1800000001"},
    {"query": "digest", "from_ts": "1800000000", "to_ts": "1799990000"},
    {"query": "digest", "from_ts": "yesterday"},
    {"query": "digest", "limit": 13}, {"query": "x" * 501},
])
def test_queries_cannot_widen_scope_or_dump_unfiltered_snapshot(arguments):
    capability, calls = capability_with_backend()
    assert not capability.query(**arguments)["ok"]
    assert not calls


def test_narrow_date_only_query_preserves_partial_coverage():
    capability, calls = capability_with_backend()
    result = capability.query(from_ts="1799990000", to_ts="1800000000")
    assert result["ok"]
    assert result["coverage"]["truncated"]
    assert result["coverage"]["next_cursor"] == "bounded-next-page"
    assert calls[0]["arguments"]["from_ts"] == "1799990000"


def test_date_only_query_can_find_rich_message_links_without_guessing_literal_phrase():
    message = provider_message(text="Research bulletin: three richly rendered article links.")
    calls = []

    def backend(payload):
        calls.append(payload)
        # Mirror the adapter's exact substring semantics. An invented semantic
        # phrase would filter out this message even when its date is correct.
        query = payload["arguments"].get("query", "")
        matching = query.casefold() in message["text"].casefold()
        return {"ok": True, "messages": [message] if matching else [], "coverage": {
            "pages": 1, "returned_count": int(matching), "query_mode": "time_interval",
        }}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    result = capability.query(from_ts="1799990000", to_ts="1800000000")
    assert result["ok"]
    assert calls[0]["arguments"]["query"] == ""
    assert calls[0]["arguments"]["thread_ts"] == ""
    assert result["messages"][0]["excerpt"] == message["text"]
    candidate = result["messages"][0]["candidate_id"]
    read = capability.read(candidate)
    assert read["messages"][0]["source_links"][2] == {
        "url": "https://example.org/article/3", "ordinal": 3,
    }


@pytest.mark.parametrize("thread_ts", ["1800000000.000001", "1800000000.000002"])
def test_triggering_request_thread_is_rejected_before_adapter(thread_ts):
    capability, calls = capability_with_backend()
    result = capability.query(query="digest", thread_ts=thread_ts)
    assert not result["ok"]
    assert result["coverage"]["errors"] == ["thread_outside_snapshot_window"]
    assert not calls
    assert capability.usage["adapter_calls"] == 0


def test_explicit_prior_thread_can_be_queried_before_snapshot_boundary():
    capability, calls = capability_with_backend()
    result = capability.query(thread_ts="1799990000.000001")
    assert result["ok"]
    assert calls[0]["arguments"]["thread_ts"] == "1799990000.000001"


@pytest.mark.parametrize("updates", [
    {"channel_id": "COTHER"}, {"message_ts": "1800000001"},
    {"message_ts": "1800000000.000001"}, {"thread_ts": "1800000000.000001"},
    {"thread_ts": "1799395199"}, {"candidate_id": ""},
])
def test_backend_candidate_scope_is_verified_before_admission(updates):
    capability, _ = capability_with_backend(provider_message(**updates))
    assert not capability.query(query="digest")["ok"]
    assert not capability.read("message:synthetic")["ok"]


def test_exact_read_must_match_prior_query_provider_identity():
    calls = []

    def backend(payload):
        calls.append(payload)
        return {"ok": True, "messages": [provider_message(**(
            {"message_ts": "1799990001.000001"} if len(calls) > 1 else {}
        ))], "coverage": {}}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    assert capability.query(query="digest")["ok"]
    assert not capability.read("message:synthetic")["ok"]


def test_query_and_read_budgets_stop_without_additional_provider_calls():
    capability, calls = capability_with_backend()
    for _ in range(2):
        assert capability.query(query="digest")["ok"]
    assert not capability.query(query="digest")["ok"]
    for _ in range(2):
        assert capability.read("message:synthetic")["ok"]
    assert not capability.read("message:synthetic")["ok"]
    assert len(calls) == 4


def test_read_retains_all_source_links_when_text_is_bounded():
    message = provider_message(text="x" * 12000)
    capability, _ = capability_with_backend(message)
    assert len(capability.query(query="digest")["messages"][0]["excerpt"]) == 1500
    read = capability.read("message:synthetic")["messages"][0]
    assert len(read["excerpt"]) == 8000
    assert read["source_links"] == message["source_links"]


@pytest.mark.parametrize("scope", [
    {}, {**descriptor(), "schema_version": 2},
    {**descriptor(), "oldest_ts": "1799395199"},
    {**descriptor(), "oldest_ts": "NaN"},
    {**descriptor(), "channel_id": "../../other"},
    {**descriptor(), "team_id": "../../other"},
    {**descriptor(), "channel_id": "D111DIRECT"},
])
def test_invalid_descriptor_attaches_no_capability(scope):
    assert channel_context_capability_from_state({"channel_context_capability": scope}) is None


@pytest.mark.parametrize("start,end,seconds", [
    ("2026-11-01T00:00:00-04:00", "2026-11-02T00:00:00-05:00", 90000),
    ("2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00", 82800),
    ("2026-11-13T00:00:00-05:00", "2026-11-14T00:00:00-05:00", 86400),
])
def test_explicit_iso_query_dates_preserve_daylight_saving_boundaries(start, end, seconds):
    from datetime import datetime
    from decimal import Decimal

    end_ts = int(datetime.fromisoformat(end).timestamp())
    scope = {**descriptor(), "request_ts": str(end_ts + 1), "oldest_ts": str(end_ts - 604799)}
    calls = []

    def backend(payload):
        calls.append(payload)
        return {"ok": True, "messages": [], "coverage": {"truncated": False}}

    capability = SlackChannelContextCapability(scope, backend=backend)
    assert capability.query(from_ts=start, to_ts=end)["ok"]
    arguments = calls[0]["arguments"]
    assert Decimal(arguments["to_ts"]) - Decimal(arguments["from_ts"]) == seconds


@pytest.mark.parametrize("value", ["2026-11-13", "2026-11-13T00:00:00", "yesterday"])
def test_query_dates_cannot_omit_timezone_or_delegate_relative_date_interpretation(value):
    capability, calls = capability_with_backend()
    result = capability.query(query="digest", from_ts=value)
    assert not result["ok"]
    assert result["coverage"]["errors"] == ["query_date_requires_timestamp_or_zoned_iso8601"]
    assert not calls


def test_failed_batch_does_not_admit_unreturned_candidate_for_later_read():
    calls = []

    def backend(payload):
        calls.append(payload)
        return {"ok": True, "messages": [provider_message(), provider_message(
            candidate_id="message:other", channel_id="COTHER",
        )], "coverage": {}}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    assert not capability.query(query="digest")["ok"]
    assert not capability.read("message:synthetic")["ok"]
    assert len(calls) == 1


def test_provider_denial_preserves_safe_code_without_provider_body():
    def backend(payload):
        return {"ok": False, "messages": [], "coverage": {"errors": [{
            "code": "channel_not_allowlisted", "type": "ContextError",
            "message": "sensitive provider details", "retry_after_seconds": 3,
        }]}}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    result = capability.query(query="digest")
    assert not result["ok"]
    assert result["coverage"]["errors"] == [{
        "code": "channel_not_allowlisted", "type": "ContextError", "retry_after_seconds": 3,
    }]
    assert "sensitive" not in str(result)


def test_read_cannot_relabel_excerpt_as_completed_message_evidence():
    calls = []

    def backend(payload):
        calls.append(payload)
        return {"ok": True, "messages": [provider_message(content_kind="excerpt")], "coverage": {}}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    assert capability.query(query="digest")["ok"]
    result = capability.read("message:synthetic")
    assert not result["ok"]
    assert result["coverage"]["errors"] == ["provider_content_kind_mismatch"]


def test_all_bounded_links_survive_separately_from_message_excerpt():
    links = [{"url": f"https://example.org/article/{index}", "ordinal": index}
             for index in range(1, 31)]
    capability, _ = capability_with_backend(provider_message(source_links=links))
    assert capability.query(query="digest")["messages"][0]["source_links"] == links
    assert capability.read("message:synthetic")["messages"][0]["source_links"] == links


def test_usage_separates_adapter_invocations_from_paginated_slack_requests():
    def backend(payload):
        return {"ok": True, "messages": [provider_message()], "coverage": {
            "pages": 5 if payload["operation"] == "query" else 1, "errors": [],
        }}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    assert capability.query(query="digest")["ok"]
    assert capability.read("message:synthetic")["ok"]
    usage = capability.usage
    assert usage["adapter_calls"] == 2
    assert usage["slack_api_calls"] == usage["slack_api_calls_observed"] == 6
    assert usage["slack_api_calls_complete"] is True
    assert "provider_calls" not in usage


def test_usage_retains_pages_from_failed_adapter_response():
    def backend(payload):
        return {"ok": False, "messages": [], "coverage": {
            "pages": 3, "errors": [{"code": "slack_rate_limited"}],
        }}

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    result = capability.query(query="digest")
    assert not result["ok"]
    assert result["coverage"]["pages"] == 3
    assert capability.usage["adapter_calls"] == 1
    assert capability.usage["slack_api_calls"] == 3
    assert capability.usage["slack_api_calls_complete"] is True


@pytest.mark.parametrize("raw", [
    {"ok": False, "coverage": {"errors": [{"code": "adapter_unavailable"}]}},
    {"ok": False, "coverage": {"pages": 0, "errors": [{"code": "slack_http_error"}]}},
    {"ok": False, "coverage": {"pages": 2, "errors": [{"code": "context_acquisition_failed"}]}},
])
def test_unknown_slack_request_count_is_not_reported_as_exact_zero(raw):
    capability = SlackChannelContextCapability(descriptor(), backend=lambda payload: raw)
    assert not capability.query(query="digest")["ok"]
    usage = capability.usage
    assert usage["adapter_calls"] == 1
    assert usage["slack_api_calls"] is None
    assert usage["slack_api_calls_complete"] is False
    assert usage["slack_api_calls_observed"] == raw["coverage"].get("pages", 0)


def test_adapter_exception_preserves_unknown_slack_usage_without_exception_details():
    def backend(payload):
        raise OSError("private provider details")

    capability = SlackChannelContextCapability(descriptor(), backend=backend)
    result = capability.query(query="digest")
    assert not result["ok"]
    assert capability.usage["adapter_calls"] == 1
    assert capability.usage["slack_api_calls"] is None
    assert capability.usage["slack_api_calls_complete"] is False
    assert "private" not in str(result)


def test_native_tool_schema_exposes_model_visible_argument_bounds():
    capability, _ = capability_with_backend()
    query_tool, read_tool = [tool.sdk_tool for tool in capability.tools]
    properties = query_tool.params_json_schema["properties"]
    assert properties["query"]["maxLength"] == 500
    assert properties["cursor"]["maxLength"] == 1024
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == 10
    candidate = read_tool.params_json_schema["properties"]["candidate_id"]
    assert candidate["minLength"] == 1
    assert candidate["maxLength"] == 160
    assert query_tool.params_json_schema["additionalProperties"] is False


@pytest.mark.parametrize("arguments,expected", [
    ({"query": "digest", "limit": 20}, {
        "field": "limit", "type": "less_than_equal", "minimum": 1, "maximum": 10,
    }),
    ({"query": "sensitive-query-" * 40}, {
        "field": "query", "type": "string_too_long", "max_length": 500,
    }),
    ({"query": "digest", "cursor": "sensitive-cursor-" * 80}, {
        "field": "cursor", "type": "string_too_long", "max_length": 1024,
    }),
])
def test_direct_argument_rejection_has_precise_safe_fields_without_spending_budget(
    arguments, expected,
):
    capability, calls = capability_with_backend()
    rejected = capability.query(**arguments)
    assert not rejected["ok"]
    assert rejected["correction"]["field_errors"] == [expected]
    assert rejected["correction"]["retryable"]
    assert not rejected["acquisition_performed"]
    assert not rejected["correction"]["acquisition_budget_consumed"]
    assert "not evidence that no messages exist" in rejected["correction"]["evidence_limitation"]
    assert "sensitive-" not in json.dumps(rejected)
    assert capability.usage["query_calls"] == 0
    assert capability.usage["argument_rejections"] == 1
    assert not calls
    assert capability.query(query="digest")["ok"]
    assert capability.query(query="digest")["ok"]
    assert capability.usage["query_calls"] == 2
    assert len(calls) == 2


def _native_tool_call(tool, arguments, call_id):
    from agents.tool_context import ToolContext

    serialized = json.dumps(arguments)
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id=call_id, tool_arguments=serialized,
    )
    return json.loads(asyncio.run(tool.on_invoke_tool(context, serialized)))


@pytest.mark.parametrize("arguments,field", [
    ({"query": "digest", "limit": 20}, "limit"),
    ({"query": "private-query-" * 50}, "query"),
    ({"query": "digest", "cursor": "private-cursor-" * 100}, "cursor"),
])
def test_native_sdk_argument_rejection_then_corrected_query_and_exact_read(arguments, field):
    capability, calls = capability_with_backend()
    query_tool, read_tool = [tool.sdk_tool for tool in capability.tools]
    rejected = _native_tool_call(query_tool, arguments, "rejected")
    assert not rejected["ok"]
    assert rejected["correction"]["field_errors"][0]["field"] == field
    assert rejected["correction"]["retryable"] is True
    assert rejected["acquisition_performed"] is False
    assert not calls
    assert capability.usage["query_calls"] == 0
    assert capability.usage["argument_rejections"] == 1
    assert "private-" not in json.dumps(rejected)

    queried = _native_tool_call(query_tool, {
        "query": "", "from_ts": "1799990000", "to_ts": "1800000000",
        "thread_ts": "", "limit": 8, "cursor": "",
    }, "corrected")
    assert queried["ok"]
    candidate = queried["messages"][0]["candidate_id"]
    read = _native_tool_call(read_tool, {"candidate_id": candidate}, "exact-read")
    assert read["ok"]
    assert read["messages"][0]["source_links"][1]["ordinal"] == 2
    assert capability.usage["query_calls"] == capability.usage["read_calls"] == 1
    assert capability.usage["adapter_calls"] == 2
    assert len(calls) == 2


def test_native_read_candidate_length_rejection_does_not_echo_identity_or_spend_budget():
    capability, calls = capability_with_backend()
    read_tool = capability.tools[1].sdk_tool
    rejected = _native_tool_call(
        read_tool, {"candidate_id": "private-identity" * 20}, "invalid-read",
    )
    assert rejected["correction"]["field_errors"] == [{
        "field": "candidate_id", "type": "string_too_long", "min_length": 1, "max_length": 160,
    }]
    assert "private-identity" not in json.dumps(rejected)
    assert capability.usage["read_calls"] == 0
    assert capability.usage["adapter_calls"] == 0
    assert not calls


def test_native_unexpected_execution_error_is_not_reported_as_cost_free_argument_rejection():
    capability, calls = capability_with_backend()
    original = capability.query

    def broken_query(**arguments):
        original(**arguments)
        raise RuntimeError("private runtime failure details")

    capability.query = broken_query
    query_tool = capability.tools[0].sdk_tool
    failed = _native_tool_call(query_tool, {"query": "digest"}, "execution-failure")
    assert not failed["ok"]
    assert failed["coverage"]["errors"] == ["channel_context_tool_execution_unavailable"]
    assert failed["acquisition_performed"] is None
    assert "correction" not in failed
    assert "private" not in json.dumps(failed)
    assert capability.usage["argument_rejections"] == 0
    assert capability.usage["query_calls"] == 1
    assert capability.usage["adapter_calls"] == 1
    assert len(calls) == 1
