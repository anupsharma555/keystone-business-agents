from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agent_tool_policy import ToolTier, tool_tier_for_name
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.entrypoints import cli_impl
from keystone_agents.gmail_triage.decision_ownership import gmail_decision_evidence
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools import gmail_query_tools
from keystone_agents.tools.gmail_query_tools import (
    GMAIL_FIXTURE_MESSAGE_ID,
    GMAIL_FIXTURE_THREAD_ID,
    GMAIL_MODEL_CONTEXT_READ_MAX,
    gmail_model_read_evidence_snapshot,
    gmail_model_read_tools,
    inspect_gmail_mailbox_schema_impl,
    query_gmail_message_summaries_impl,
    read_gmail_context_impl,
)
from keystone_agents.tools.gmail_tool import GmailConfigurationError


def _tool_names(agent: Any) -> set[str]:
    return {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in agent.tools}


def test_gmail_schema_declares_bounded_read_only_contract() -> None:
    schema = inspect_gmail_mailbox_schema_impl()

    assert schema.schema_name == "keystone.gmail.read_schema.v1"
    assert schema.query_parameters == ["query", "label", "max_results"]
    assert [field.name for field in schema.query_parameter_details] == [
        "query",
        "label",
        "max_results",
    ]
    assert schema.execution_parameters == ["live"]
    assert [field.name for field in schema.execution_parameter_details] == ["live"]
    assert (
        "KEYSTONE_ENABLE_LIVE_GMAIL=true" in (schema.execution_parameter_details[0].constraints[1])
    )
    assert all(field.purpose for field in schema.query_parameter_details)
    assert schema.label_parameter_contract == "provider_label_id_or_system_label"
    assert schema.max_query_results == 20
    assert schema.live_environment_gate == "KEYSTONE_ENABLE_LIVE_GMAIL"
    assert schema.read_only is True
    assert schema.sends_email is False
    assert schema.writes_mailbox_state is False
    assert schema.raw_message_bodies_returned is False
    assert {resource.resource_type for resource in schema.resources} == {
        "message_summary",
        "message_context",
        "thread_context",
    }


def test_gmail_schema_renderer_includes_all_four_model_visible_inputs() -> None:
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id="gmail-schema-call",
                tool_name="inspect_gmail_mailbox_schema",
            ),
            SimpleNamespace(
                type="tool_call_output_item",
                call_id="gmail-schema-call",
                output=json.dumps(inspect_gmail_mailbox_schema_impl().model_dump(mode="json")),
            ),
        ]
    )

    summary = cli_impl._gmail_schema_summary_from_tool_output(raw_result)

    assert summary.count("\n- `") == 3
    assert "- `query`:" in summary
    assert "- `label`:" in summary
    assert "- `max_results`:" in summary
    assert "- `live`:" in summary
    assert "Never authorizes mailbox writes or sends" in summary


def test_gmail_query_dry_run_returns_labeled_synthetic_fixture() -> None:
    result = query_gmail_message_summaries_impl(
        query="newer_than:7d partnership",
        label="INBOX",
        max_results=5,
    )

    assert result.status == "fixture"
    assert result.provider_read_performed is False
    assert result.provider_write_performed is False
    assert result.raw_message_bodies_returned is False
    assert result.item_count == 1
    assert result.items[0].message_id == GMAIL_FIXTURE_MESSAGE_ID
    assert result.items[0].thread_id == GMAIL_FIXTURE_THREAD_ID
    assert "Synthetic fixture only" in result.limitations[0]


@pytest.mark.parametrize("max_results", [0, 21])
def test_gmail_query_rejects_unbounded_result_counts(max_results: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 20"):
        query_gmail_message_summaries_impl(max_results=max_results)


def test_gmail_live_query_requires_existing_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_search_message_summaries(**_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_GMAIL", raising=False)
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        fake_search_message_summaries,
    )

    with pytest.raises(GmailConfigurationError, match="KEYSTONE_ENABLE_LIVE_GMAIL=true"):
        query_gmail_message_summaries_impl(live=True)
    assert called is False


def test_gmail_live_query_reuses_provider_summary_method_and_omits_raw_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [
            {
                "id": "msg-1",
                "threadId": "thread-1",
                "received_at": "2026-08-02T14:00:00Z",
                "sender_name": "Alex",
                "sender_email": "alex@example.com",
                "to": "private@example.com",
                "subject": "Follow-up",
                "snippet": "Could you review this?",
                "labelIds": ["INBOX", "UNREAD"],
                "body": "must not escape",
                "normalized_body": "must not escape",
            }
        ]

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        fake_search_message_summaries,
    )

    result = query_gmail_message_summaries_impl(
        query="newer_than:3d",
        label="INBOX",
        max_results=3,
        live=True,
    )

    assert captured == {"label": "INBOX", "max_results": 3, "query": "newer_than:3d"}
    assert result.status == "read"
    assert result.provider_read_performed is True
    assert result.item_count == 1
    payload = result.model_dump(mode="json")
    assert "body" not in payload["items"][0]
    assert "to" not in payload["items"][0]
    assert "must not escape" not in str(payload)


def test_gmail_exact_context_dry_run_requires_fixture_identity() -> None:
    message = read_gmail_context_impl(
        resource_type="message",
        resource_id=GMAIL_FIXTURE_MESSAGE_ID,
    )
    thread = read_gmail_context_impl(
        resource_type="thread",
        resource_id=GMAIL_FIXTURE_THREAD_ID,
    )
    missing = read_gmail_context_impl(resource_type="message", resource_id="unknown")

    assert message.status == "fixture"
    assert message.message is not None
    assert message.message.message_id == GMAIL_FIXTURE_MESSAGE_ID
    assert thread.status == "fixture"
    assert thread.resource_id == GMAIL_FIXTURE_THREAD_ID
    assert missing.status == "not_found"
    assert all(result.raw_message_bodies_returned is False for result in (message, thread, missing))


def test_gmail_exact_message_context_projects_safe_fields_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get_message(message_id: str) -> dict[str, Any]:
        assert message_id == "msg-1"
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "received_at": "2026-08-02T14:00:00Z",
            "sender_name": "Alex",
            "sender_email": "alex@example.com",
            "to": "private@example.com",
            "subject": "Follow-up",
            "snippet": "Could you review this?",
            "body": "private full body",
            "normalized_body": "private full body",
            "thread_summary": "A review was requested.",
            "thread_context": "Bounded context.",
            "suspicious_signals": [],
            "attachment_metadata": [
                {
                    "filename": "brief.pdf",
                    "size_bytes": 123,
                    "attachment_id": "private-provider-id",
                    "raw_content": "must not escape",
                }
            ],
            "triage_limitations": ["Attachments were not ingested."],
        }

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        fake_get_message,
    )

    result = read_gmail_context_impl(
        resource_type="message",
        resource_id="msg-1",
        live=True,
    )

    payload = result.model_dump(mode="json")
    assert result.status == "read"
    assert result.provider_read_performed is True
    assert result.message is not None
    assert result.message.message_id == "msg-1"
    assert "body" not in payload
    assert "private full body" not in str(payload)
    assert "to" not in payload["message"]
    assert "private-provider-id" not in str(payload)
    assert "raw_content" not in str(payload)


def test_gmail_thread_context_independently_bounds_provider_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    very_long = "private detail " * 1_000

    def fake_get_thread(thread_id: str) -> dict[str, Any]:
        assert thread_id == "thread-1"
        return {
            "id": "thread-1",
            "message_count": 1_000,
            "subject": very_long,
            "summary": very_long,
            "thread_context": very_long,
            "latest_received_at": very_long,
            "participants": [very_long] * 30,
            "action_items": [very_long] * 20,
            "deadlines": [very_long] * 20,
            "open_questions": [very_long] * 20,
            "triage_limitations": [very_long] * 40,
            "messages": [{"body": "complete body must not escape"}],
        }

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "get_thread_with_source_url", fake_get_thread)

    result = read_gmail_context_impl(
        resource_type="thread",
        resource_id="thread-1",
        live=True,
    )
    payload = result.model_dump(mode="json")

    assert result.message_count == 100
    assert len(result.subject) <= 300
    assert len(result.summary) <= 1_200
    assert len(result.thread_context) <= 1_500
    assert len(result.latest_received_at) <= 40
    assert len(result.participants) <= 12
    assert all(len(item) <= 200 for item in result.participants)
    assert len(result.action_items) <= 5
    assert all(len(item) <= 500 for item in result.action_items)
    assert len(result.deadlines) <= 5
    assert len(result.open_questions) <= 5
    assert len(result.triage_limitations) <= 20
    assert "complete body must not escape" not in str(payload)


def test_gmail_exact_context_rejects_provider_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        lambda _message_id: {"id": "different", "threadId": "thread-1"},
    )

    result = read_gmail_context_impl(
        resource_type="message",
        resource_id="requested",
        live=True,
    )

    assert result.status == "not_found"
    assert "exact requested message identity" in result.triage_limitations[0]


@pytest.mark.parametrize("provider_payload", [{}, {"id": "different"}])
def test_gmail_exact_thread_context_requires_matching_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    provider_payload: dict[str, str],
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda _thread_id: provider_payload,
    )

    result = read_gmail_context_impl(
        resource_type="thread",
        resource_id="requested",
        live=True,
    )

    assert result.status == "not_found"
    assert result.resource_id == "requested"
    assert "exact requested thread identity" in result.triage_limitations[0]


def test_gmail_query_tools_are_core_read_and_attached_to_registered_agent_builder() -> None:
    expected = {
        "inspect_gmail_mailbox_schema",
        "query_gmail_message_summaries",
        "read_gmail_context",
    }

    assert {tool_tier_for_name(name) for name in expected} == {ToolTier.CORE_READ}
    default_agent = build_gmail_triage_agent()
    assert expected <= _tool_names(default_agent)
    assert expected <= _tool_names(build_gmail_triage_agent(tool_tier="core_read"))

    tools_by_name = {tool.name: tool for tool in default_agent.tools}
    query_schema = tools_by_name["query_gmail_message_summaries"].params_json_schema
    read_schema = tools_by_name["read_gmail_context"].params_json_schema
    assert query_schema["properties"]["query"]["maxLength"] == 500
    assert query_schema["properties"]["label"]["maxLength"] == 100
    assert "provider label ID" in query_schema["properties"]["label"]["description"]
    assert "Leave empty" in query_schema["properties"]["label"]["description"]
    assert "archived and sent mail" in query_schema["properties"]["label"]["description"]
    assert query_schema["properties"]["max_results"] == {
        "default": 10,
        "maximum": 20,
        "minimum": 1,
        "title": "Max Results",
        "type": "integer",
    }
    assert read_schema["properties"]["resource_id"]["maxLength"] == 200
    assert read_schema["properties"]["resource_id"]["minLength"] == 1


def test_model_visible_gmail_tools_bind_execution_mode_outside_the_schema() -> None:
    live_tools = {tool.name: tool for tool in gmail_model_read_tools(live=True)}
    fixture_tools = {tool.name: tool for tool in gmail_model_read_tools(live=False)}

    for tools in (live_tools, fixture_tools):
        query_schema = tools["query_gmail_message_summaries"].sdk_tool.params_json_schema
        read_schema = tools["read_gmail_context"].sdk_tool.params_json_schema
        assert "live" not in query_schema["properties"]
        assert "live" not in read_schema["properties"]

    assert "live_provider" in live_tools["query_gmail_message_summaries"].sdk_tool.description
    assert "fixture" in fixture_tools["query_gmail_message_summaries"].sdk_tool.description


def test_bound_gmail_context_windows_advance_and_cache_by_exact_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    full_text = "first window | second window | final qualification: NOT approved"
    account_hash = "a" * 64
    snapshot_hash = "b" * 64
    provider_reads: list[dict[str, Any]] = []

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [
            {
                "id": "msg-windowed",
                "threadId": "thread-windowed",
                "subject": "Windowed source",
                "snippet": "first window",
            }
        ],
    )

    def projection(message_id: str, **kwargs: Any) -> dict[str, Any]:
        assert message_id == "msg-windowed"
        provider_reads.append(dict(kwargs))
        start = int(kwargs.get("body_start_char") or 0)
        maximum = int(kwargs.get("max_body_chars") or 20)
        end = min(len(full_text), start + maximum)
        has_more = end < len(full_text)
        next_request = (
            {
                "resource_type": "message",
                "resource_id": message_id,
                "body_part_path": "0",
                "body_start_char": end,
                "max_body_chars": maximum,
                "expected_thread_id": "thread-windowed",
                "expected_account_identity_sha256": account_hash,
                "expected_source_snapshot_sha256": snapshot_hash,
            }
            if has_more
            else None
        )
        return {
            "status": "read",
            "id": message_id,
            "threadId": "thread-windowed",
            "account_identity_sha256": account_hash,
            "provider_history_id": "history-windowed",
            "source_snapshot_sha256": snapshot_hash,
            "source_restart_required": False,
            "subject": "Windowed source",
            "snippet": "first window",
            "thread_summary": "Windowed source summary.",
            "thread_context": "first window",
            "source_url": (
                "https://mail.google.com/mail/?authuser=reader%40example.test"
                "#all/thread-windowed"
            ),
            "body_evidence": [
                {
                    "part_path": "0",
                    "mime_type": "text/plain",
                    "representation": "plain",
                    "role": "single_representation",
                    "source_text": full_text[start:end],
                    "content_complete": not has_more,
                    "truncated": has_more,
                    "coverage": {
                        "start_char": start,
                        "end_char": end,
                        "full_char_count": len(full_text),
                        "complete": not has_more,
                        "has_more": has_more,
                    },
                    "next_request": next_request,
                }
            ],
            "body_content_status": "partial" if has_more else "complete",
            "body_content_complete": not has_more,
            "provider_read": True,
            "triage_limitations": [],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        projection,
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}
    json.loads(tools["query_gmail_message_summaries"](query="windowed source"))
    first = json.loads(
        tools["read_gmail_context"](
            resource_type="message",
            resource_id="msg-windowed",
            max_body_chars=20,
        )
    )
    continuation = first["body_evidence"][0]["next_request"]
    second = json.loads(tools["read_gmail_context"](**continuation))
    repeated = json.loads(tools["read_gmail_context"](**continuation))

    assert [item.get("body_start_char", 0) for item in provider_reads] == [0, 20]
    assert second["body_evidence"][0]["coverage"]["start_char"] == 20
    assert repeated["provider_read_performed"] is False
    assert repeated["context_cache_reused"] is True
    ledger = gmail_model_read_evidence_snapshot(tool_list)
    evidence = gmail_decision_evidence(
        SimpleNamespace(new_items=[]),
        cumulative_tool_evidence=ledger,
    )
    assert evidence.context_read_call_count == 1
    assert evidence.context_read_output_count == 1
    assert evidence.context_provider_read_count == 2
    assert evidence.model_context_read_call_count == 3


def test_gmail_provider_selection_mode_forces_only_the_first_query_tool() -> None:
    agent = build_gmail_triage_agent(
        provider_tools_live=True,
        provider_selection_mode=True,
        request_text="Find the current conversation and decide whether I owe a reply.",
    )

    assert _tool_names(agent) == {
        "query_gmail_message_summaries",
        "read_gmail_context",
    }
    assert agent.model_settings.tool_choice == "query_gmail_message_summaries"
    assert agent.reset_tool_choice is True


def test_gmail_model_tools_preserve_bounded_evidence_across_one_corrective_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    query_calls: list[str] = []
    context_reads: list[str] = []

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        query_calls.append(query)
        if query == "G2i interview":
            return [
                {
                    "id": "msg-reminder",
                    "threadId": "thread-reminder",
                    "subject": "G2i interview reminder",
                    "snippet": "Reminder for the upcoming conversation.",
                }
            ]
        return [
            {
                "id": "msg-current",
                "threadId": "thread-current",
                "subject": "G2i interview confirmation",
                "snippet": "The current confirmed interview time.",
            }
        ]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": thread_id,
            "summary": f"Bounded context for {thread_id}.",
            "thread_context": f"Verified context for {thread_id}.",
            "participants": ["Synthetic Recruiting"],
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "get_thread_with_source_url", get_thread)
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    json.loads(tools["query_gmail_message_summaries"](query="G2i interview"))
    json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-reminder",
        )
    )
    json.loads(tools["query_gmail_message_summaries"](query="G2i confirmed interview time"))
    json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-current",
        )
    )

    evidence = gmail_model_read_evidence_snapshot(tool_list)

    assert query_calls == ["G2i interview", "G2i confirmed interview time"]
    assert context_reads == ["thread-reminder", "thread-current"]
    assert [entry["tool_name"] for entry in evidence] == [
        "query_gmail_message_summaries",
        "read_gmail_context",
        "query_gmail_message_summaries",
        "read_gmail_context",
    ]
    assert evidence[0]["output"]["items"][0]["thread_id"] == "thread-reminder"
    assert evidence[2]["output"]["items"][0]["thread_id"] == "thread-current"


def test_gmail_query_result_exposes_remaining_capacity_for_exact_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [
            {
                "id": "msg-exact",
                "threadId": "thread-exact",
                "subject": "Exact subject",
                "snippet": "Exact bounded candidate.",
            }
        ],
    )
    tools = {tool.name: tool for tool in gmail_model_read_tools(live=True)}

    with activate_model_request_budget(3) as ledger:
        ledger.consume(stage="gmail_triage:query")
        payload = json.loads(
            tools["query_gmail_message_summaries"](query='subject:"Exact subject"')
        )

    capacity = payload["model_request_capacity"]
    assert capacity["remaining_model_requests"] == 2
    assert capacity["required_future_requests"] == 2
    assert capacity["capacity_sufficient"] is True
    assert capacity["candidate_read_and_final_allowed"] is True
    assert capacity["corrective_query_allowed"] is False
    assert capacity["final_response_allowed"] is True
    assert capacity["status"] == "candidate_read_and_final_admitted"


def test_gmail_corrective_query_reports_final_only_capacity_without_forcing_a_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    provider_queries: list[str] = []

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        provider_queries.append(str(kwargs.get("query") or ""))
        return [
            {
                "id": "msg-irrelevant",
                "threadId": "thread-irrelevant",
                "subject": "Adjacent result",
                "snippet": "Not enough to answer the current request.",
            }
        ]

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    with activate_model_request_budget(3) as ledger:
        ledger.consume(stage="gmail_triage:first_query")
        first = json.loads(
            tools["query_gmail_message_summaries"](query="Example Health pilot")
        )
        ledger.consume(stage="gmail_triage:corrective_query")
        second = json.loads(
            tools["query_gmail_message_summaries"](query="Example Health ORBIT")
        )
        remaining = ledger.remaining

    assert provider_queries == ["Example Health pilot", "Example Health ORBIT"]
    assert first["model_request_capacity"]["corrective_query_allowed"] is False
    assert second["provider_read_performed"] is True
    assert second["model_request_capacity"]["remaining_model_requests"] == 1
    assert second["model_request_capacity"]["capacity_sufficient"] is False
    assert second["model_request_capacity"]["candidate_read_and_final_allowed"] is False
    assert second["model_request_capacity"]["corrective_query_allowed"] is False
    assert second["model_request_capacity"]["final_response_allowed"] is True
    assert second["model_request_capacity"]["status"] == "final_response_only"
    assert remaining == 1
    assert [entry["tool_name"] for entry in gmail_model_read_evidence_snapshot(tool_list)] == [
        "query_gmail_message_summaries",
        "query_gmail_message_summaries",
    ]


def test_gmail_corrective_query_runs_when_read_and_final_capacity_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    provider_queries: list[str] = []
    context_reads: list[str] = []

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        suffix = "current" if "ORBIT" in query else "adjacent"
        return [
            {
                "id": f"msg-{suffix}",
                "threadId": f"thread-{suffix}",
                "subject": query,
                "snippet": f"Bounded {suffix} candidate.",
            }
        ]

    def get_thread(thread_id: str) -> dict[str, Any]:
        context_reads.append(thread_id)
        return {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": "Example Health ORBIT",
            "summary": "Verified current context.",
            "thread_context": "Verified current context.",
        }

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        get_thread,
    )
    tools = {tool.name: tool for tool in gmail_model_read_tools(live=True)}

    with activate_model_request_budget(4) as ledger:
        ledger.consume(stage="gmail_triage:first_query")
        first = json.loads(
            tools["query_gmail_message_summaries"](query="Example Health pilot")
        )
        ledger.consume(stage="gmail_triage:corrective_query")
        second = json.loads(
            tools["query_gmail_message_summaries"](query="Example Health ORBIT")
        )
        ledger.consume(stage="gmail_triage:context_read")
        context = json.loads(
            tools["read_gmail_context"](
                resource_type="thread",
                resource_id="thread-current",
            )
        )
        remaining = ledger.remaining

    assert first["model_request_capacity"]["corrective_query_allowed"] is True
    assert second["query"] == "Example Health ORBIT"
    assert second["model_request_capacity"]["candidate_read_and_final_allowed"] is True
    assert second["model_request_capacity"]["capacity_sufficient"] is True
    assert context["model_request_capacity"]["must_return_final_response_now"] is True
    assert provider_queries == ["Example Health pilot", "Example Health ORBIT"]
    assert context_reads == ["thread-current"]
    assert remaining == 1


def test_gmail_model_tools_reuse_identical_query_then_allow_one_distinct_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    provider_queries: list[str] = []

    def search_message_summaries(**kwargs: Any) -> list[dict[str, Any]]:
        query = str(kwargs.get("query") or "")
        provider_queries.append(query)
        return []

    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        search_message_summaries,
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    json.loads(tools["query_gmail_message_summaries"](query="G2i interview"))
    repeated = json.loads(
        tools["query_gmail_message_summaries"](query="G2i interview")
    )
    json.loads(
        tools["query_gmail_message_summaries"](
            query="from:(g2i.co) interview confirmation"
        )
    )

    ledger = gmail_model_read_evidence_snapshot(tool_list)
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id=f"query-{index}",
                tool_name="query_gmail_message_summaries",
            )
            for index in range(1, 4)
        ]
    )
    evidence = gmail_decision_evidence(
        raw_result,
        cumulative_tool_evidence=ledger,
    )

    assert provider_queries == [
        "G2i interview",
        "from:(g2i.co) interview confirmation",
    ]
    assert repeated["provider_read_performed"] is False
    assert repeated["query_cache_reused"] is True
    assert "distinct corrective query" in " ".join(repeated["limitations"])
    assert [entry["tool_name"] for entry in ledger] == [
        "query_gmail_message_summaries",
        "query_gmail_message_summaries_repeated",
        "query_gmail_message_summaries",
    ]
    assert evidence.query_call_count == 2
    assert evidence.model_query_call_count == 3
    assert evidence.repeated_query_call_count == 1
    assert evidence.corrective_query_count == 1
    assert all("query" not in attempt for attempt in evidence.query_attempts)
    assert all("returned_candidate_ids" not in attempt for attempt in evidence.query_attempts)


def test_gmail_model_tools_allow_two_corrections_then_block_fourth_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    provider_queries: list[str] = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **kwargs: provider_queries.append(str(kwargs.get("query") or "")) or [],
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    first = json.loads(tools["query_gmail_message_summaries"](query="first"))
    second = json.loads(tools["query_gmail_message_summaries"](query="second"))
    third = json.loads(tools["query_gmail_message_summaries"](query="third"))
    blocked = json.loads(
        tools["query_gmail_message_summaries"](query="fourth")
    )

    assert provider_queries == ["first", "second", "third"]
    assert [r["remaining_query_calls"] for r in [first, second, third]] == [2, 1, 0]
    assert blocked["provider_read_performed"] is False
    assert blocked["query_budget_blocked"] is True
    assert gmail_model_read_evidence_snapshot(tool_list)[-1]["tool_name"] == (
        "query_gmail_message_summaries_blocked"
    )


def test_gmail_model_tools_block_fifth_context_before_provider_read_and_trace_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    candidates = [
        {
            "id": f"msg-{index}",
            "threadId": f"thread-{index}",
            "subject": f"Candidate {index}",
            "snippet": f"Bounded candidate summary {index}.",
        }
        for index in range(1, 6)
    ]
    provider_reads: list[str] = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(candidates),
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda thread_id: provider_reads.append(thread_id)
        or {
            "thread_id": thread_id,
            "message_count": 1,
            "subject": thread_id,
            "summary": f"Bounded context for {thread_id}.",
            "thread_context": f"Verified context for {thread_id}.",
            "participants": ["Synthetic Recruiting"],
        },
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    json.loads(tools["query_gmail_message_summaries"](query="interview candidates"))
    for index in range(1, GMAIL_MODEL_CONTEXT_READ_MAX + 1):
        context = json.loads(
            tools["read_gmail_context"](
                resource_type="thread",
                resource_id=f"thread-{index}",
            )
        )
        assert context["provider_read_performed"] is True

    cached = json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-1",
        )
    )
    blocked = json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-5",
        )
    )

    assert provider_reads == [f"thread-{index}" for index in range(1, 5)]
    assert cached["provider_read_performed"] is False
    assert cached["context_cache_reused"] is True
    assert blocked["provider_read_performed"] is False
    assert blocked["context_read_budget_blocked"] is True
    assert blocked["recoverable"] is True
    assert blocked["reason_code"] == "gmail_context_read_budget_exhausted"
    assert blocked["context_read_limit"] == 4
    assert blocked["completed_context_read_count"] == 4
    assert "Decide using the four verified contexts" in " ".join(
        blocked["triage_limitations"]
    )

    ledger = gmail_model_read_evidence_snapshot(tool_list)
    assert ledger[-2]["tool_name"] == "read_gmail_context_repeated"
    assert ledger[-1]["tool_name"] == "read_gmail_context_blocked"
    assert ledger[-1]["arguments"] == {
        "resource_type": "thread",
        "resource_id": "thread-5",
    }
    raw_result = SimpleNamespace(
        new_items=[
            SimpleNamespace(
                type="tool_call_item",
                call_id=f"read-{index}",
                tool_name="read_gmail_context",
            )
            for index in range(1, 7)
        ]
    )
    evidence = gmail_decision_evidence(
        raw_result,
        cumulative_tool_evidence=ledger,
    )
    assert evidence.context_read_call_count == 4
    assert evidence.context_provider_read_count == 4
    assert evidence.model_context_read_call_count == 6
    assert evidence.repeated_context_read_call_count == 1
    assert evidence.blocked_context_read_call_count == 1
    assert evidence.read_context_summaries == tuple(
        {
            "resource_type": "thread",
            "resource_id": f"thread-{index}",
            "subject": f"thread-{index}",
            "summary": f"Bounded context for thread-{index}.",
            "thread_context": f"Verified context for thread-{index}.",
            "latest_received_at": "",
            "messages": [],
            "source_url": "",
            "triage_limitations": [
                "Complete per-message bodies are intentionally omitted "
                "from model-visible tool output.",
                "Some thread messages are not represented in the timeline.",
            ],
            "participants": ["Synthetic Recruiting"],
        }
        for index in range(1, 5)
    )


def test_unknown_gmail_candidate_is_rejected_without_consuming_context_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    provider_reads: list[str] = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [
            {
                "id": "msg-known",
                "threadId": "thread-known",
                "subject": "Known candidate",
                "snippet": "Bounded candidate summary.",
            }
        ],
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda thread_id: provider_reads.append(thread_id) or {},
    )
    tool_list = gmail_model_read_tools(live=True)
    tools = {tool.name: tool for tool in tool_list}

    json.loads(tools["query_gmail_message_summaries"](query="interview"))
    rejected = json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-fabricated",
        )
    )
    ledger = gmail_model_read_evidence_snapshot(tool_list)
    evidence = gmail_decision_evidence(
        SimpleNamespace(new_items=[]),
        cumulative_tool_evidence=ledger,
    )

    assert rejected["status"] == "not_found"
    assert provider_reads == []
    assert ledger[-1]["tool_name"] == "read_gmail_context_rejected"
    assert evidence.context_read_call_count == 0
    assert evidence.context_provider_read_count == 0
    assert evidence.model_context_read_call_count == 1
    assert evidence.rejected_context_read_call_count == 1


def test_bound_gmail_read_rejects_unknown_query_identity_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [
            {
                "id": "msg-known",
                "threadId": "thread-known",
                "subject": "Known conversation",
                "snippet": "Bounded summary.",
            }
        ],
    )
    provider_reads: list[str] = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda thread_id: provider_reads.append(thread_id) or {},
    )
    tools = {tool.name: tool for tool in gmail_model_read_tools(live=True)}

    json.loads(tools["query_gmail_message_summaries"](query="known"))
    output = json.loads(
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-unknown",
        )
    )

    assert output["status"] == "not_found"
    assert output["provider_read_performed"] is False
    assert provider_reads == []


def test_bound_gmail_read_redacts_secrets_but_still_blocks_phi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    values = [
        {
            "id": "msg-phi",
            "threadId": "thread-phi",
            "subject": "Clinical context",
            "snippet": "Bounded summary.",
        }
    ]
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: list(values),
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        lambda _thread_id: {
            "thread_id": "thread-phi",
            "message_count": 1,
            "subject": "Patient Jane Doe diagnosis follow-up",
            "summary": "token=redactionfixture123",
            "thread_context": "Patient Jane Doe was diagnosed with depression.",
            "participants": ["Example Clinician"],
        },
    )
    tools = {tool.name: tool for tool in gmail_model_read_tools(live=True)}
    json.loads(tools["query_gmail_message_summaries"](query="clinical"))

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        tools["read_gmail_context"](
            resource_type="thread",
            resource_id="thread-phi",
        )


def test_subject_query_repair_keeps_model_choice_and_explains_mailbox_scope(monkeypatch):
    calls = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    def search(**kwargs):
        calls.append(kwargs)
        return []
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", search)
    result = query_gmail_message_summaries_impl(
        query='from:account_alias subject:"Example update"', label="", live=True,
    )
    assert calls[0]["query"] == 'from:account_alias subject:"Example update"'
    assert result.item_count == 0 and result.provider_read_performed
    guidance = " ".join(result.limitations)
    assert "mailbox is separate from the sender" in guidance
    assert "does not establish" in guidance
    # A legitimate self-sent-message filter is not silently stripped by Python.
    query_gmail_message_summaries_impl(query="from:owner@example.test", live=True)
    assert calls[-1]["query"] == "from:owner@example.test"
    tool = next(t for t in gmail_model_read_tools(live=True)
                if t.name == "query_gmail_message_summaries")
    assert "account name is not a sender" in tool.sdk_tool.description


@pytest.mark.parametrize("compact", [False, True])
def test_search_policy_is_present_in_both_gmail_execution_profiles(compact):
    agent = build_gmail_triage_agent(compact_instructions=compact, include_tools=True)
    instructions = " ".join(str(agent.instructions).split())
    assert "Do not include the mailbox account name as a free-text search term" in instructions
    assert "Gmail normally combines space-separated terms with AND" in instructions
    assert "Distinguish when the email arrived from dates mentioned inside it" in instructions
    query_tool = next(tool for tool in agent.tools if tool.name == "query_gmail_message_summaries")
    description = query_tool.params_json_schema["properties"]["query"]["description"]
    assert "Space-separated terms are ANDed" in description


def test_empty_free_text_query_recovers_quoted_anchor_and_discloses_both_reads(monkeypatch):
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    calls = []
    original = '"software workshop" city uncertain'

    def search(**kwargs):
        calls.append(kwargs)
        return [] if kwargs["query"] == original else [{
            "id": "candidate", "threadId": "thread", "subject": "Software workshop invitation",
        }]

    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", search)
    result = query_gmail_message_summaries_impl(
        query=original, label="CATEGORY_UPDATES", max_results=3, live=True,
    )
    assert result.query == original
    assert result.executed_queries == [original, '"software workshop"']
    assert result.item_count == 1 and result.items[0].message_id == "candidate"
    assert all(c["label"] == "CATEGORY_UPDATES" and c["max_results"] == 3 for c in calls)
    assert "may not match every original clue" in " ".join(result.limitations)


@pytest.mark.parametrize("query", [
    'from:sender@example.test "software workshop" other',
    'after:2026/01/01 "software workshop" other',
    'label:Important "software workshop" other',
    'subject:"software workshop" other',
    '"software workshop" -cancelled',
    '"software workshop" +confirmed',
    '"software workshop" AROUND 5 keynote',
    '"software workshop" OR "design meeting" other',
    '"software workshop"',
    'software workshop city',
    '"software workshop',
])
def test_anchor_recovery_never_relaxes_explicit_search_scope_or_invents_terms(monkeypatch, query):
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    calls = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool, "search_message_summaries",
        lambda **kwargs: calls.append(kwargs["query"]) or [],
    )
    result = query_gmail_message_summaries_impl(query=query, live=True)
    assert calls == [query]
    assert result.executed_queries == [query]


def test_anchor_recovery_stops_after_one_bounded_fallback(monkeypatch):
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    calls = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool, "search_message_summaries",
        lambda **kwargs: calls.append(kwargs["query"]) or [],
    )
    result = query_gmail_message_summaries_impl(query='"software workshop" unknown', live=True)
    assert calls == ['"software workshop" unknown', '"software workshop"']
    assert result.item_count == 0


def test_anchor_recovery_preserves_multiple_quoted_literals():
    assert gmail_query_tools._quoted_anchor_query(
        '"research and development" "software workshop" unknown city'
    ) == '"research and development" "software workshop"'


def test_nonempty_or_malformed_provider_results_do_not_trigger_relaxation(monkeypatch):
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    calls = []
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool, "search_message_summaries",
        lambda **kwargs: calls.append(kwargs["query"]) or [{"unexpected": "value"}],
    )
    query_gmail_message_summaries_impl(query='"software workshop" unknown', live=True)
    assert calls == ['"software workshop" unknown']


def test_failed_anchor_provider_read_is_not_reported_as_no_matches(monkeypatch):
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    calls = []
    def search(**kwargs):
        calls.append(kwargs["query"])
        if len(calls) == 2:
            raise ConnectionError("provider unavailable")
        return []
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", search)
    with pytest.raises(ConnectionError, match="provider unavailable"):
        query_gmail_message_summaries_impl(query='"software workshop" unknown', live=True)
    assert len(calls) == 2


def test_thread_context_preserves_dated_senders_without_quote_body_duplication():
    raw = {
        "thread_id": "selected", "message_count": 3,
        "source_url": "https://mail.google.com/mail/#all/selected",
        "messages": [
            {"id": "followup", "threadId": "selected", "received_at": "2026-03-01T12:00:00Z",
             "sender_name": "Operator", "snippet": "Following up on my request.",
             "body": "UNEXPOSED_FULL_BODY"},
            {"id": "welcome", "threadId": "selected", "received_at": "2026-01-01T12:00:00Z",
             "sender_name": "Community",
             "snippet": "Welcome; let us know if you need invitations."},
            {"id": "request", "threadId": "selected", "received_at": "2026-02-01T12:00:00Z",
             "sender_name": "Operator", "snippet": "Please add me to the invitations."},
            {"id": "foreign", "threadId": "other", "received_at": "2026-04-01T12:00:00Z",
             "sender_name": "Unrelated", "snippet": "Not this thread."},
        ],
    }
    context = gmail_query_tools._thread_context_from_provider("selected", raw)
    assert [m.message_id for m in context.messages] == ["welcome", "request", "followup"]
    assert context.messages[-1].sender_name == "Operator"
    assert context.messages[0].received_at < context.messages[-1].received_at
    assert context.source_url == raw["source_url"]
    assert "UNEXPOSED_FULL_BODY" not in context.model_dump_json()
    assert "Unrelated" not in context.model_dump_json()


def test_thread_timeline_reports_incomplete_dates_and_bounded_history():
    context = gmail_query_tools._thread_context_from_provider("selected", {
        "thread_id": "selected", "message_count": 13,
        "messages": [{"id": str(i), "threadId": "selected", "received_at": "unknown"}
                     for i in range(13)],
    })
    assert len(context.messages) == 12
    limitations = " ".join(context.triage_limitations)
    assert "chronology is incomplete" in limitations and "order is uncertain" in limitations
    assert "most recent" not in limitations


def test_thread_source_url_comes_from_authenticated_account(monkeypatch):
    calls = []
    class ReadOnlyThread:
        def get_thread(self, *, thread_id):
            calls.append(("thread", thread_id))
            return {"status": "read", "thread_id": thread_id}
        def current_account_email(self):
            calls.append(("profile",))
            return "reader@example.test"
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "_gmail_read_tool", ReadOnlyThread)
    result = gmail_query_tools.gmail_tool.get_thread_with_source_url("selected")
    assert result["source_url"] == (
        "https://mail.google.com/mail/?authuser=reader%40example.test#all/selected"
    )
    assert calls == [("thread", "selected"), ("profile",)]
