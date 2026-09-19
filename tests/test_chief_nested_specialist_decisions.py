from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from keystone_agents.agent_decision_contracts import chief_of_staff_decision_contract
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.receipts.mutations import operation_is_mutation
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.airtable import airtable_schema_snapshot_sha256
from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefSpecialistToolInput,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import (
    announcement_context_tools,
    gmail_query_tools,
    internal_data_tools,
    zotero_context_tools,
)
from keystone_agents.tools.gmail_tool import GmailTool

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.tool_context import ToolContext
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")


class FakeModel(Model):
    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "system_instructions": system_instructions,
                "tool_choice": getattr(model_settings, "tool_choice", None),
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"chief-nested-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


@dataclass(frozen=True)
class NestedCase:
    route: str
    tool_name: str
    read_tool_name: str
    read_arguments: dict[str, Any]
    actual_id: str
    alternative_id: str
    decision_stage: str


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> Any:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> Any:
    return ResponseOutputMessage(
        id="chief-nested-output",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=json.dumps(payload),
                annotations=[],
            )
        ],
    )


def _decision(
    case: NestedCase,
    *,
    selected_id: str | None = None,
    assessed_ids: tuple[str, ...] | None = None,
    stage: str | None = None,
) -> dict[str, Any]:
    selected = selected_id or case.actual_id
    assessed = assessed_ids or (case.actual_id, case.alternative_id)
    return {
        "decision_owner": "specialist_agent",
        "decision_stage": stage or case.decision_stage,
        "selected_candidate_ids": [selected],
        "candidate_assessments": [
            {
                "candidate_id": candidate_id,
                "disposition": "selected" if candidate_id == selected else "excluded",
                "rationale": (
                    "Best current match from bounded provider evidence."
                    if candidate_id == selected
                    else "Plausible but older or less relevant alternative."
                ),
            }
            for candidate_id in assessed
        ],
        "reasoning": "Selected the strongest current match from the provider result.",
        "limitations": ["Synthetic bounded provider metadata only."],
        "needs_more_context": False,
    }


def _payload(
    case: NestedCase,
    *,
    decision: dict[str, Any] | None,
) -> dict[str, Any]:
    common = {
        "mode": "llm",
        "summary": "Selected the strongest current bounded context object.",
    }
    if case.route == "airtable_context_agent":
        payload = {
            **common,
            "base_alias": "eval_tracker",
            "relevant_tables": ["Eval Runs"],
            "candidate_record_ids": [case.actual_id, case.alternative_id],
            "recommended_record_identity": case.actual_id,
        }
    elif case.route == "google_workspace_context_agent":
        payload = {
            **common,
            "relevant_files": [case.actual_id, case.alternative_id],
            "recommended_target": case.actual_id,
        }
    elif case.route == "zotero_context_agent":
        payload = {
            **common,
            "library_context": "Synthetic read-only library",
            "article_titles": ["Current evidence review"],
            "zotero_item_keys": [case.actual_id],
            "source_ids": [case.alternative_id],
        }
    else:
        selected = {
            "feed_item_id": case.actual_id,
            "title": "Current behavioral-health validation signal",
            "url": "https://example.com/current-signal",
            "source": "synthetic feed",
            "summary": "A current bounded validation signal.",
            "published_at": "2026-08-03",
            "relevance_status": "selected",
            "selection_reason": "Best match.",
        }
        payload = {
            **common,
            "query": "behavioral health validation",
            "retrieved_item_ids": [case.actual_id],
            "articles": [selected],
        }
    if decision is not None:
        payload["decision"] = decision
    return payload


def _chief_parent_payload(*, valid_decision: bool) -> dict[str, Any]:
    selected_id = (
        "workflow:research-direction-review"
        if valid_decision
        else "workflow:clarification"
    )
    return {
        "agent_name": "chief_of_staff",
        "mode": "llm",
        "intent": "Review bounded provider context and recommend the next workflow.",
        "summary": "Chief reviewed the validated nested specialist context.",
        "recommended_route": {
            "workflow_type": "research-direction-review",
            "rationale": "The bounded context supports a research direction review.",
            "requires_live_connector": False,
            "requires_human_approval_before_post": True,
        },
        "approval_required": True,
        "human_review_required": True,
        "send_enabled": False,
        "slack_post_allowed": False,
        "slack_post_policy": "not_allowed",
        "decision": {
            "decision_owner": "chief_of_staff",
            "decision_stage": "chief_delegation_selection",
            "selected_candidate_id": selected_id,
            "candidate_assessments": [
                {
                    "candidate_id": selected_id,
                    "disposition": "selected",
                    "rationale": "Selected from the bounded Chief workflow candidates.",
                }
            ],
            "reasoning": "Use the validated nested evidence without another child read.",
            "limitations": ["The child result is advisory and read-only."],
            "needs_more_context": False,
        },
    }


def _configure_case(monkeypatch: pytest.MonkeyPatch, route: str) -> NestedCase:
    if route == "airtable_context_agent":
        case = NestedCase(
            route=route,
            tool_name="airtable_context_agent_as_specialist_tool",
            read_tool_name="airtable_read_records",
            read_arguments={
                "table": "Eval Runs",
                "base_alias": "eval_tracker",
                "max_records": 4,
                "live": False,
            },
            actual_id="rec-current-private",
            alternative_id="rec-older-private",
            decision_stage="airtable_record_selection",
        )
        monkeypatch.setattr(
            internal_data_tools,
            "airtable_read_records_impl",
            lambda *_args, **_kwargs: {
                "status": "success",
                "provider_read": True,
                "records": [
                    {
                        "id": case.actual_id,
                        "fields": {
                            "Name": "Current evaluation run",
                            "Status": "Ready",
                        },
                    },
                    {
                        "id": case.alternative_id,
                        "fields": {
                            "Name": "Older evaluation run",
                            "Status": "Done",
                        },
                    },
                ],
                "identity_fingerprints": identity_fingerprints(
                    [case.actual_id, case.alternative_id]
                ),
                "send_enabled": False,
            },
        )
        return case
    if route == "google_workspace_context_agent":
        case = NestedCase(
            route=route,
            tool_name="google_workspace_context_agent_as_specialist_tool",
            read_tool_name="google_drive_search_files",
            read_arguments={
                "query": "quarterly review",
                "folder_path": "KNIOps",
                "max_items": 5,
                "live": False,
            },
            actual_id="drive-current-private",
            alternative_id="drive-older-private",
            decision_stage="workspace_artifact_selection",
        )
        monkeypatch.setattr(
            internal_data_tools,
            "google_drive_search_files_impl",
            lambda *_args, **_kwargs: {
                "status": "success",
                "operation": "search_files",
                "provider_read": True,
                "items": [
                    {"id": case.actual_id, "name": "Quarterly Review Current"},
                    {"id": case.alternative_id, "name": "Quarterly Review Archive"},
                ],
                "identity_fingerprints": identity_fingerprints(
                    [case.actual_id, case.alternative_id]
                ),
                "send_enabled": False,
            },
        )
        return case
    if route == "zotero_context_agent":
        case = NestedCase(
            route=route,
            tool_name="zotero_context_agent_as_specialist_tool",
            read_tool_name="zotero_read_api_metadata",
            read_arguments={
                "query": "evidence review",
                "limit": 5,
                "item_type": "journalArticle",
                "selection_count": 2,
                "live": True,
            },
            actual_id="ZOTERO-CURRENT-PRIVATE",
            alternative_id="ZOTERO-OLDER-PRIVATE",
            decision_stage="zotero_item_selection",
        )
        monkeypatch.setenv("ZOTERO_API_KEY", "synthetic-test-key")
        monkeypatch.setenv("ZOTERO_LIBRARY_ID", "synthetic-library")
        monkeypatch.setattr(
            zotero_context_tools,
            "_read_zotero_api_json",
            lambda *_args, **_kwargs: [
                {
                    "key": case.actual_id,
                    "data": {"key": case.actual_id, "title": "Current evidence review"},
                },
                {
                    "key": case.alternative_id,
                    "data": {"key": case.alternative_id, "title": "Older evidence review"},
                },
            ],
        )
        return case
    preprints = route == "preprints_context_agent"
    case = NestedCase(
        route=route,
        tool_name=f"{route}_as_specialist_tool",
        read_tool_name=(
            "retrieve_preprint_announcement_history"
            if preprints
            else "retrieve_rss_announcement_history"
        ),
        read_arguments={
            "query": "behavioral health validation",
            "selected_only": None,
            "limit": 8,
            **({} if preprints else {"live": False}),
        },
        actual_id=f"{'preprint' if preprints else 'rss'}-current-private",
        alternative_id=f"{'preprint' if preprints else 'rss'}-older-private",
        decision_stage="signal_relevance_selection",
    )
    items = [
        {
            "feed_item_id": case.actual_id,
            "title": "Current behavioral-health validation signal",
            "url": "https://example.com/current-signal",
            "source": "synthetic feed",
            "summary": "A current bounded validation signal.",
            "published_at": "2026-08-03",
        },
        {
            "feed_item_id": case.alternative_id,
            "title": "Older general signal",
            "url": "https://example.com/older-signal",
            "source": "synthetic feed",
            "summary": "An older general signal.",
            "published_at": "2026-07-01",
        },
    ]
    implementation_name = (
        "retrieve_preprint_announcement_history_impl"
        if preprints
        else "retrieve_rss_announcement_history_impl"
    )
    monkeypatch.setattr(
        announcement_context_tools,
        implementation_name,
        lambda **_kwargs: {
            "status": "success",
            "kind": "preprints" if preprints else "rss",
            "items": items,
            "item_count": 2,
            "send_enabled": False,
        },
    )
    return case


def _invoke_case(
    case: NestedCase,
    model: FakeModel,
    *,
    nested_live_execution: bool = False,
) -> tuple[dict[str, Any], Any]:
    chief = build_chief_of_staff_agent(
        include_specialist_tools=True,
        nested_live_execution=nested_live_execution,
    )
    tool = next(item for item in chief.tools if getattr(item, "name", "") == case.tool_name)
    tool_input = ChiefSpecialistToolInput(
        raw_operator_request=f"Review bounded {case.route} context without changing it.",
        specialist_task="Read the current candidates and select the strongest match.",
        decision_context={"success_criteria": "provider-bound selection"},
        provider_call_context={"provider": case.route, "operation": "read"},
        side_effect_boundaries=["no_nested_live_write"],
    ).model_dump(mode="json")
    call_id = f"chief-{case.route}-call"
    context = ToolContext(
        context=None,
        run_config=build_local_run_config(FakeProvider(model)),
        tool_name=tool.name,
        tool_call_id=call_id,
        tool_arguments=json.dumps(tool_input),
    )
    envelope = json.loads(
        asyncio.run(tool.on_invoke_tool(context, json.dumps(tool_input)))
    )
    internal = context._custom_data["keystone_nested_specialist_execution"]
    assert internal == tool.nested_execution_records[call_id]
    assert "keystone_nested_specialist_execution" not in json.dumps(envelope)
    return envelope, tool


def _nested_gmail_case() -> NestedCase:
    return NestedCase(
        route="gmail_triage",
        tool_name="gmail_triage_as_specialist_tool",
        read_tool_name="query_gmail_message_summaries",
        read_arguments={"query": "subject:review", "max_results": 2},
        actual_id="thread-current-private",
        alternative_id="thread-older-private",
        decision_stage="gmail_candidate_selection",
    )


def _nested_gmail_payload(case: NestedCase, *, selected_id: str | None = None) -> dict:
    return {
        "thread_id": selected_id or case.actual_id,
        "category": "collaboration_opportunity",
        "confidence": 0.9,
        "summary": "The current review is ready for the operator's consideration.",
        "reasoning": "Compared both returned conversations and selected the current one.",
        "recommended_action": "Review the current conversation without changing Gmail.",
        "decision": _decision(case, selected_id=selected_id),
    }


@pytest.mark.parametrize(("repair", "reject"), [(False, False), (True, False), (True, True)])
def test_nested_gmail_uses_real_provider_mode_and_validates_before_returning_to_chief(
    monkeypatch: pytest.MonkeyPatch, repair: bool, reject: bool
) -> None:
    case = _nested_gmail_case()
    reads: list[str] = []
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    def query(**_kwargs: Any) -> list[dict[str, Any]]:
        reads.append("query")
        return [
            {"id": f"message-{index}", "threadId": identity, "subject": "Review"}
            for index, identity in enumerate((case.actual_id, case.alternative_id))
        ]

    def read_thread_with_source_url(identity: str) -> dict[str, Any]:
        reads.append(identity)
        return {
            "thread_id": identity,
            "message_count": 1,
            "subject": "Review",
            "summary": "Current review" if identity == case.actual_id else "Older review",
        }

    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", query)
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_thread_with_source_url",
        read_thread_with_source_url,
    )
    final_payload = _nested_gmail_payload(case, selected_id="invented-thread" if reject else None)
    initial_payload = (
        _nested_gmail_payload(case, selected_id="invented-thread") if repair else final_payload
    )
    responses = [
        [_tool_call(case.read_tool_name, case.read_arguments, call_id="gmail-query")],
        [
            _tool_call(
                "read_gmail_context",
                {"resource_type": "thread", "resource_id": identity},
                call_id=f"gmail-read-{index}",
            )
            for index, identity in enumerate((case.actual_id, case.alternative_id))
        ],
        [_structured_message(initial_payload)],
    ]
    if repair:
        responses.append(
            [
                _structured_message(
                    {"repair": {"resolution": "selected", "triage_result": final_payload}}
                )
            ]
        )
    model = FakeModel(responses)

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)

    diagnostics = {item["key"]: item["value"] for item in envelope["diagnostics"]}
    record = tool.nested_execution_records["chief-gmail_triage-call"]
    assert reads[0] == "query"
    assert sorted(reads[1:]) == sorted([case.actual_id, case.alternative_id])
    assert record["candidate_universe"] == [case.actual_id, case.alternative_id]
    assert record["usage"]["requests"] == 3 + int(repair)
    assert record["repair_attempts"] == int(repair)
    assert model.calls[0]["tool_names"] == [
        "query_gmail_message_summaries", "read_gmail_context"
    ]
    assert model.calls[0]["tool_choice"] == "query_gmail_message_summaries"
    assert all(
        not operation_is_mutation(name) for call in model.calls for name in call["tool_names"]
    )
    assert case.actual_id not in json.dumps(envelope)
    if repair:
        assert model.calls[-1]["tool_names"] == []
        assert case.actual_id in json.dumps(model.calls[-1]["input"], default=str)
        assert case.alternative_id in json.dumps(model.calls[-1]["input"], default=str)
    if reject:
        assert envelope["validation_status"] == "blocked"
        assert record["handoff"]["terminal_status"] == "blocked"
        assert record["decision_ownership"]["validator_outcome"]["status"] != "accepted"
        assert "invented-thread" not in json.dumps(envelope)
        return
    assert diagnostics["validator_status"] == "accepted"
    assert diagnostics["nested_execution_mode"] == "live_read_only"
    assert record["selected_identity_fingerprints"] == identity_fingerprints([case.actual_id])
    # Parent repair or duplicate delegation must consume the verified envelope,
    # not spend another Gmail query or child model run.
    replay_input = ChiefSpecialistToolInput(specialist_task="Reuse the selected context.")
    replay_context = ToolContext(
        context=None,
        run_config=build_local_run_config(FakeProvider(model)),
        tool_name=tool.name,
        tool_call_id="gmail-replay",
        tool_arguments=replay_input.model_dump_json(),
    )
    replay = json.loads(
        asyncio.run(tool.on_invoke_tool(replay_context, replay_input.model_dump_json()))
    )
    assert replay == envelope
    assert len(reads) == 3
    assert len(model.calls) == 3 + int(repair)


def test_nested_live_gmail_missing_gate_cannot_return_a_fixture_answer(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_GMAIL", raising=False)
    case = _nested_gmail_case()
    model = FakeModel(
        [
            [_tool_call(case.read_tool_name, case.read_arguments, call_id="gmail-query")],
            [_structured_message(_nested_gmail_payload(case))],
            [_structured_message(_nested_gmail_payload(case))],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)

    assert envelope["validation_status"] == "blocked"
    record = tool.nested_execution_records["chief-gmail_triage-call"]
    assert record["handoff"]["terminal_status"] == "blocked"
    assert record["candidate_universe"] == []


def test_nested_gmail_sdk_input_preserves_mime_facts_and_quote_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = NestedCase(
        route="gmail_triage",
        tool_name="gmail_triage_as_specialist_tool",
        read_tool_name="query_gmail_message_summaries",
        read_arguments={"query": "subject:pilot", "max_results": 2},
        actual_id="thread-source-private",
        alternative_id="thread-other-private",
        decision_stage="gmail_candidate_selection",
    )

    def part(mime_type: str, text: str) -> dict[str, Any]:
        return {
            "mimeType": mime_type,
            "body": {
                "data": base64.urlsafe_b64encode(text.encode()).decode(),
            },
        }

    def projection(
        message_id: str,
        thread_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(payload)
        payload["headers"] = [
            {"name": "Subject", "value": "Pilot source evidence"},
            {"name": "From", "value": "Publisher <publisher@example.test>"},
        ]
        raw = {
            "id": message_id,
            "threadId": thread_id,
            "internalDate": "1788278400000",
            "snippet": "Pilot source evidence",
            "payload": payload,
        }
        tool = GmailTool(live=True, access_token="synthetic-unused-token")
        monkeypatch.setattr(tool, "_request", lambda *_args, **_kwargs: raw)
        monkeypatch.setattr(tool, "current_account_email", lambda: "reader@example.test")
        return tool.get_message_context_projection(message_id)

    selected_projection = projection(
        "msg-source-private",
        case.actual_id,
        {
            "mimeType": "multipart/alternative",
            "parts": [
                part("text/plain", "View this message in an HTML-capable reader."),
                part(
                    "text/html",
                    "<p>Pilot update.</p><blockquote>The pilot is NOT approved; "
                    "budget is $0.</blockquote>",
                ),
            ],
        },
    )
    other_projection = projection(
        "msg-other-private",
        case.alternative_id,
        part("text/plain", "Unrelated archived update."),
    )
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [
            {
                "id": "msg-source-private",
                "threadId": case.actual_id,
                "subject": "Pilot source evidence",
            },
            {
                "id": "msg-other-private",
                "threadId": case.alternative_id,
                "subject": "Archived update",
            },
        ],
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        lambda identity: (
            selected_projection if identity == "msg-source-private" else other_projection
        ),
    )
    output = _nested_gmail_payload(case)
    model = FakeModel(
        [
            [_tool_call(case.read_tool_name, case.read_arguments, call_id="gmail-query")],
            [
                _tool_call(
                    "read_gmail_context",
                    {"resource_type": "message", "resource_id": message_id},
                    call_id=f"gmail-read-{message_id}",
                )
                for message_id in ("msg-source-private", "msg-other-private")
            ],
            [_structured_message(output)],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)
    specialist_input = json.dumps(model.calls[2]["input"], default=str)
    record = tool.nested_execution_records["chief-gmail_triage-call"]

    assert envelope["validation_status"] == "ok", json.dumps(record, default=str)
    assert "msg-source-private" in specialist_input
    assert case.actual_id in specialist_input
    assert "text/plain" in specialist_input
    assert "text/html" in specialist_input
    assert "editorial_source" in specialist_input
    assert "NOT approved" in specialist_input
    assert "$0" in specialist_input
    assert "materially richer HTML" in specialist_input
    assert record["candidate_universe"] == [case.actual_id, case.alternative_id]
    assert record["tool_origins"][0]["model_called_tool_names"] == [
        "query_gmail_message_summaries",
        "read_gmail_context",
    ]
    assert record["handoff"]["terminal_status"] == "completed"


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    ],
)
def test_chief_nested_context_specialists_preserve_validated_child_decisions(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    case = _configure_case(monkeypatch, route)
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id=f"{route}-read",
                )
            ],
            [_structured_message(_payload(case, decision=_decision(case)))],
        ]
    )

    envelope, tool = _invoke_case(case, model)

    diagnostics = {item["key"]: item["value"] for item in envelope["diagnostics"]}
    record = tool.nested_execution_records[f"chief-{route}-call"]
    assert envelope["parsed_output_status"] == "parsed"
    assert diagnostics["validator_status"] == "accepted"
    assert diagnostics["nested_execution_state"] == "executed_nested_specialist"
    assert diagnostics["nested_consumption_status"] == "returned_to_chief_model"
    assert case.actual_id not in json.dumps(envelope)
    assert case.alternative_id not in json.dumps(envelope)
    assert record["raw_structured_decision"]["selected_candidate_ids"] == [
        case.actual_id
    ]
    assert record["candidate_universe"] == [case.actual_id, case.alternative_id]
    assert record["candidate_identity_fingerprints"]
    assert record["handoff"]["state"] == "executed"
    assert record["handoff"]["terminal_status"] == "completed"
    assert record["usage"]["requests"] == 2
    assert record["tool_origins"][0]["model_called_tool_names"] == [
        case.read_tool_name
    ]


def test_chief_nested_airtable_specialist_receives_exact_schema_detail_and_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = NestedCase(
        route="airtable_context_agent",
        tool_name="airtable_context_agent_as_specialist_tool",
        read_tool_name="airtable_read_records",
        read_arguments={
            "table": "Evidence",
            "base_id": "appSynthetic",
            "max_records": 2,
            "fetch_all": True,
            "live": True,
        },
        actual_id="rec-airtable-current",
        alternative_id="rec-airtable-older",
        decision_stage="airtable_record_selection",
    )
    long_description = "Review context. " * 36 + "NOT approved for external use."
    schema_payload = {
        "tables": [
            {
                "id": "tblEvidence",
                "name": "Evidence",
                "primaryFieldId": "fldName",
                "fields": [
                    {"id": "fldName", "name": "Name", "type": "singleLineText"},
                    {
                        "id": "fldDecision",
                        "name": "Decision —  source",
                        "type": "formula",
                        "description": long_description,
                        "options": {
                            "formula": 'IF({fldState}="Needs — Review","Hold  now","Proceed")',
                            "isValid": True,
                            "referencedFieldIds": ["fldState"],
                            "result": {
                                "type": "currency",
                                "options": {"precision": 2, "symbol": "£"},
                            },
                        },
                    },
                ],
            }
        ]
    }
    snapshot = airtable_schema_snapshot_sha256(schema_payload)
    records_payload = {
        "records": [
            {
                "id": case.actual_id,
                "fields": {
                    "Amount": 0,
                    "Flag": False,
                    "Related": [],
                    "Status": "NOT approved",
                },
            },
            {
                "id": case.alternative_id,
                "fields": {"Amount": -4, "Related": ["recLinked"]},
            },
        ]
    }

    def fake_send(request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if "/meta/bases/" in str(request.get("url") or ""):
            return schema_payload
        return records_payload

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appSynthetic")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_synthetic")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Evidence")
    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_send)
    model = FakeModel(
        [
            [
                _tool_call(
                    "airtable_get_base_schema",
                    {"base_id": "appSynthetic", "live": True},
                    call_id="airtable-schema",
                )
            ],
            [
                _tool_call(
                    "airtable_read_schema_detail",
                    {
                        "base_id": "appSynthetic",
                        "read_mode": "field_detail",
                        "table_id": "tblEvidence",
                        "field_id": "fldDecision",
                        "max_chars": 2_000,
                        "expected_source_sha256": snapshot,
                        "live": True,
                    },
                    call_id="airtable-schema-detail",
                )
            ],
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="airtable-records",
                )
            ],
            [_structured_message(_payload(case, decision=_decision(case)))],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)
    schema_input = json.dumps(model.calls[1]["input"], default=str)
    detail_input = json.dumps(model.calls[2]["input"], default=str)
    decision_input = json.dumps(model.calls[3]["input"], default=str)
    record = tool.nested_execution_records["chief-airtable_context_agent-call"]

    assert envelope["validation_status"] == "ok", json.dumps(record, default=str)
    assert "Decision" in schema_input
    assert "Hold  now" in schema_input
    assert "NOT approved for external use." in detail_input
    assert case.actual_id in decision_input
    assert case.alternative_id in decision_input
    assert "NOT approved" in decision_input
    assert record["tool_origins"][0]["model_called_tool_names"] == [
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
        "airtable_read_records",
    ]
    assert record["handoff"]["terminal_status"] == "completed"
    assert record["usage"]["requests"] == 4


def test_chief_nested_rss_specialist_receives_late_saved_evidence_before_decision(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'nested-rss-evidence.db'}"
    item = AnnouncementFeedItem(
        canonical_key="rss-nested-evidence",
        title="Nested RSS evidence review",
        url="https://example.org/nested-rss",
        source="rss",
        feed="rss",
        summary="A preliminary saved signal.",
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    title=f"Background {index}",
                    url=f"https://example.org/background/{index}",
                    snippet="Background only.",
                )
                for index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                title="Qualified result",
                url="https://example.org/qualified-result",
                snippet="Late qualification: NOT approved; external validation is absent.",
                status="success",
            ),
        ],
    )
    SQLiteStore(database_url).save_announcement_feed_item(item)
    monkeypatch.setenv("DATABASE_URL", database_url)
    history = announcement_context_tools.retrieve_rss_announcement_history_impl(
        query=item.title,
        database_url=database_url,
    )
    evidence_id = history["items"][0]["evidence_index"][3]["evidence_id"]
    case = NestedCase(
        route="rss_context_agent",
        tool_name="rss_context_agent_as_specialist_tool",
        read_tool_name="retrieve_rss_announcement_history",
        read_arguments={
            "query": item.title,
            "history_scope": "discovery",
            "limit": 8,
            "live": False,
        },
        actual_id=item.canonical_key,
        alternative_id="unused",
        decision_stage="signal_relevance_selection",
    )
    payload = {
        "mode": "llm",
        "summary": "The saved evidence says the signal is not approved.",
        "query": item.title,
        "retrieved_item_ids": [item.canonical_key],
        "articles": [
            {
                "feed_item_id": item.canonical_key,
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "feed": item.feed,
                "summary": item.summary,
                "detailed_summary": (
                    "The saved snippet says NOT approved and external validation is "
                    "absent. Full article coverage is not proven."
                ),
                "relevance_status": "selected",
                "selection_reason": "Exact saved evidence was read.",
                "evidence_status": "article_extracted",
                "limitations": ["Saved snippet only; full article not verified."],
            }
        ],
        "decision": _decision(case, assessed_ids=(item.canonical_key,)),
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="rss-history",
                )
            ],
            [
                _tool_call(
                    "read_rss_announcement_evidence",
                    {
                        "feed_item_id": item.canonical_key,
                        "evidence_id": evidence_id,
                        "max_chars": 1000,
                    },
                    call_id="rss-saved-evidence",
                )
            ],
            [_structured_message(payload)],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)
    discovery_input = json.dumps(model.calls[1]["input"], default=str)
    decision_input = json.dumps(model.calls[2]["input"], default=str)
    record = tool.nested_execution_records["chief-rss_context_agent-call"]

    assert envelope["validation_status"] == "ok", json.dumps(record, default=str)
    assert evidence_id in discovery_input
    assert "NOT approved" not in discovery_input
    assert "Late qualification: NOT approved" in decision_input
    assert "external validation is absent" in decision_input
    assert record["candidate_universe"] == [item.canonical_key]
    assert record["tool_origins"][0]["model_called_tool_names"] == [
        case.read_tool_name,
        "read_rss_announcement_evidence",
    ]
    assert record["tool_origins"][0]["model_tool_call_count"] == 2
    assert record["handoff"]["terminal_status"] == "completed"


def test_chief_nested_google_doc_handoff_sees_table_facts_and_visual_limitations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Request:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def execute(self) -> dict[str, Any]:
            return self.payload

    class _Documents:
        def get(self, **kwargs: Any) -> _Request:
            assert kwargs == {
                "documentId": "doc-structured-private",
                "includeTabsContent": True,
                "suggestionsViewMode": "SUGGESTIONS_INLINE",
            }
            return _Request(document)

    class _Docs:
        def documents(self) -> _Documents:
            return _Documents()

    document = {
        "title": "Structured pilot plan",
        "revisionId": "revision-handoff-3",
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-main", "title": "Main"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {"textRun": {"content": "Pilot overview\n"}}
                                    ]
                                }
                            },
                            {
                                "table": {
                                    "rows": 2,
                                    "columns": 2,
                                    "tableRows": [
                                        {
                                            "tableCells": [
                                                {
                                                    "content": [
                                                        {
                                                            "paragraph": {
                                                                "elements": [
                                                                    {
                                                                        "textRun": {
                                                                            "content": "Status\n"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    ]
                                                },
                                                {
                                                    "content": [
                                                        {
                                                            "paragraph": {
                                                                "elements": [
                                                                    {
                                                                        "textRun": {
                                                                            "content": "Budget\n"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "tableCells": [
                                                {
                                                    "content": [
                                                        {
                                                            "paragraph": {
                                                                "elements": [
                                                                        {
                                                                            "textRun": {
                                                                                "content": (
                                                                                    "NOT approved\n"
                                                                                )
                                                                            }
                                                                        }
                                                                ]
                                                            }
                                                        }
                                                    ]
                                                },
                                                {
                                                    "content": [
                                                        {
                                                            "paragraph": {
                                                                "elements": [
                                                                    {
                                                                        "textRun": {
                                                                            "content": "$0\n"
                                                                        }
                                                                    }
                                                                ]
                                                            }
                                                        }
                                                    ]
                                                },
                                            ]
                                        },
                                    ],
                                }
                            },
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "inlineObjectElement": {
                                                "inlineObjectId": "image-1"
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    },
                    "inlineObjects": {"image-1": {"inlineObjectProperties": {}}},
                },
                "childTabs": [],
            }
        ],
    }
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"docs": _Docs(), "drive": object()},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_in_folder",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_start_google_workspace_read_attempt",
        lambda: None,
    )
    monkeypatch.setenv(
        internal_data_tools.GOOGLE_WORKSPACE_LIVE_READS_ENV,
        "true",
    )

    case = NestedCase(
        route="google_workspace_context_agent",
        tool_name="google_workspace_context_agent_as_specialist_tool",
        read_tool_name="google_doc_read",
        read_arguments={
            "document_id_or_url": "doc-structured-private",
            "folder_path": "KNIOps",
            "max_chars": 6000,
            "live": False,
        },
        actual_id="doc-structured-private",
        alternative_id="unused",
        decision_stage="workspace_artifact_selection",
    )
    output = {
        "mode": "llm",
        "summary": (
            "The source table says the pilot is NOT approved with a $0 budget; "
            "one inline visual was not read."
        ),
        "relevant_docs": [case.actual_id],
        "recommended_target": case.actual_id,
        "media_context_limitations": ["One inline visual was not read."],
        "sources": [
            {
                "source_id": case.actual_id,
                "title": "Structured pilot plan",
                "source_type": "google_doc",
                "location": (
                    "https://docs.google.com/document/d/doc-structured-private/edit"
                ),
                "note": "Table text read; inline visual unread.",
            }
        ],
        "diagnostics": [
            {"key": "content_complete", "value": "false"},
            {"key": "visual_content_status", "value": "not_read"},
        ],
        "decision": _decision(case, assessed_ids=(case.actual_id,)),
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="google-doc-read",
                )
            ],
            [_structured_message(output)],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)
    child_turn = model.calls[1]["input"]
    child_input = json.dumps(child_turn, default=str)
    tool_output_item = next(
        item
        for item in child_turn
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    tool_output = json.loads(tool_output_item["output"])
    record = tool.nested_execution_records["chief-google_workspace_context_agent-call"]

    assert envelope["validation_status"] == "ok", json.dumps(record, default=str)
    assert "Review bounded google_workspace_context_agent context" in child_input
    assert "doc-structured-private" in child_input
    assert "https://docs.google.com/document/d/doc-structured-private/edit" in child_input
    assert "revision-handoff-3" in child_input
    assert (
        "[[table 1 cell row=2 cell_index=1 row_span=1 column_span=1]]"
        in child_input
    )
    assert "NOT approved" in child_input
    assert (
        "[[table 1 cell row=2 cell_index=2 row_span=1 column_span=1]]"
        in child_input
    )
    assert "$0" in child_input
    assert tool_output["content_complete"] is False
    assert tool_output["visual_content_status"] == "not_read"
    assert tool_output["source_version"]["revision_id"] == "revision-handoff-3"
    assert "Inline drawings or images are present" in child_input
    assert envelope["summary"].startswith("The source table says the pilot is NOT approved")
    assert envelope["parsed_output_status"] == "parsed"
    assert record["handoff"]["consumption_status"] == "returned_to_chief_model"
    assert record["tool_origins"][0]["model_called_tool_names"] == ["google_doc_read"]
    assert record["candidate_universe"] == [case.actual_id]


def test_nested_google_doc_continuation_reaches_semantics_and_late_qualifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[dict[str, Any]] = []

    class _Request:
        def execute(self) -> dict[str, Any]:
            return document

    class _Documents:
        def get(self, **kwargs: Any) -> _Request:
            provider_calls.append(kwargs)
            return _Request()

    class _Docs:
        def documents(self) -> _Documents:
            return _Documents()

    document = {
        "title": "Long semantic source",
        "revisionId": "revision-semantic-handoff-5",
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-source", "title": "Source"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "startIndex": 1,
                                            "endIndex": 9,
                                            "textRun": {
                                                "content": "Approved",
                                                "textStyle": {"strikethrough": True},
                                            },
                                        },
                                        {
                                            "startIndex": 9,
                                            "endIndex": 16,
                                            "textRun": {
                                                "content": " Source",
                                                "textStyle": {
                                                    "link": {
                                                        "url": (
                                                            "https://example.org/"
                                                            "handoff-proof"
                                                        )
                                                    }
                                                },
                                            },
                                        },
                                    ]
                                }
                            },
                            {
                                "paragraph": {
                                    "elements": [
                                        {"textRun": {"content": "A" * 1200}}
                                    ]
                                }
                            },
                            {
                                "paragraph": {
                                    "elements": [
                                        {
                                            "textRun": {
                                                "content": (
                                                    "Late qualifier: NOT approved; budget "
                                                    "is $0."
                                                )
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"docs": _Docs(), "drive": object()},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_in_folder",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_start_google_workspace_read_attempt",
        lambda: None,
    )
    monkeypatch.setenv(internal_data_tools.GOOGLE_WORKSPACE_LIVE_READS_ENV, "true")

    case = NestedCase(
        route="google_workspace_context_agent",
        tool_name="google_workspace_context_agent_as_specialist_tool",
        read_tool_name="google_doc_read",
        read_arguments={
            "document_id_or_url": "doc-semantic-private",
            "folder_path": "KNIOps",
            "max_chars": 1000,
            "live": False,
        },
        actual_id="doc-semantic-private",
        alternative_id="unused",
        decision_stage="workspace_artifact_selection",
    )
    output = {
        "mode": "llm",
        "summary": (
            "The struck wording links to the source; the later qualifier says NOT "
            "approved with a $0 budget."
        ),
        "relevant_docs": [case.actual_id],
        "recommended_target": case.actual_id,
        "sources": [
            {
                "source_id": case.actual_id,
                "title": "Long semantic source",
                "source_type": "google_doc",
                "location": (
                    "https://docs.google.com/document/d/doc-semantic-private/edit"
                ),
                "note": "Two revision-pinned bounded windows read.",
            }
        ],
        "diagnostics": [
            {"key": "revision_id", "value": "revision-semantic-handoff-5"},
            {"key": "continuation_used", "value": "true"},
        ],
        "decision": _decision(case, assessed_ids=(case.actual_id,)),
    }
    continuation_arguments = {
        **case.read_arguments,
        "start_char": 1000,
        "semantic_start": 2,
        "expected_revision_id": "revision-semantic-handoff-5",
    }
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="google-doc-first-window",
                )
            ],
            [
                _tool_call(
                    case.read_tool_name,
                    continuation_arguments,
                    call_id="google-doc-second-window",
                )
            ],
            [_structured_message(output)],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)
    first_tool_turn = json.dumps(model.calls[1]["input"], default=str)
    final_specialist_input = json.dumps(model.calls[2]["input"], default=str)
    first_tool_output = json.loads(
        next(
            item["output"]
            for item in model.calls[1]["input"]
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )
    second_tool_output = json.loads(
        [
            item["output"]
            for item in model.calls[2]["input"]
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ][-1]
    )
    record = tool.nested_execution_records["chief-google_workspace_context_agent-call"]

    assert envelope["validation_status"] == "ok", json.dumps(record, default=str)
    assert "revision-semantic-handoff-5" in first_tool_turn
    assert "https://example.org/handoff-proof" in first_tool_turn
    assert first_tool_output["semantic_annotations"][0]["strikethrough"] is True
    assert first_tool_output["continuation"]["next_request"]["start_char"] == 1000
    assert second_tool_output["read_window"]["start_char"] == 1000
    assert "Late qualifier: NOT approved; budget is $0." in second_tool_output["text"]
    assert "Late qualifier: NOT approved; budget is $0." in final_specialist_input
    assert "chars:1000-" in final_specialist_input
    assert provider_calls == [
        {
            "documentId": "doc-semantic-private",
            "includeTabsContent": True,
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
        },
        {
            "documentId": "doc-semantic-private",
            "includeTabsContent": True,
            "suggestionsViewMode": "SUGGESTIONS_INLINE",
        },
    ]
    assert record["usage"]["requests"] == 3
    assert record["candidate_universe"] == [case.actual_id]
    assert record["handoff"]["terminal_status"] == "completed"


@pytest.mark.parametrize(
    ("route", "invalid_kind"),
    [
        ("airtable_context_agent", "fabricated"),
        ("google_workspace_context_agent", "omitted_alternative"),
        ("zotero_context_agent", "missing_decision"),
    ],
)
def test_chief_nested_context_decision_repair_is_tool_free_and_evidence_preserving(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    invalid_kind: str,
) -> None:
    case = _configure_case(monkeypatch, route)
    if invalid_kind == "fabricated":
        invalid_decision = _decision(case, selected_id="fabricated-private-id")
    elif invalid_kind == "omitted_alternative":
        invalid_decision = _decision(case, assessed_ids=(case.actual_id,))
    else:
        invalid_decision = None
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id=f"{route}-read",
                )
            ],
            [_structured_message(_payload(case, decision=invalid_decision))],
            [_structured_message(_payload(case, decision=_decision(case)))],
        ]
    )

    envelope, tool = _invoke_case(case, model)

    diagnostics = {item["key"]: item["value"] for item in envelope["diagnostics"]}
    record = tool.nested_execution_records[f"chief-{route}-call"]
    assert diagnostics["validator_status"] == "accepted"
    assert diagnostics["repair_attempts"] == "1"
    assert len(model.calls) == 3
    assert model.calls[2]["tool_names"] == []
    assert case.actual_id in json.dumps(model.calls[2]["input"], default=str)
    assert case.alternative_id in json.dumps(model.calls[2]["input"], default=str)
    assert record["repair_attempts"] == 1
    assert record["decision_ownership"]["repair_evidence"][
        "provider_calls_during_repair"
    ] == 0


def test_chief_parent_decision_repair_reuses_validated_child_without_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, "airtable_context_agent")
    provider_reads = 0
    provider_read = internal_data_tools.airtable_read_records_impl

    def counted_provider_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal provider_reads
        provider_reads += 1
        return provider_read(*args, **kwargs)

    monkeypatch.setattr(
        internal_data_tools,
        "airtable_read_records_impl",
        counted_provider_read,
    )
    child_input = ChiefSpecialistToolInput(
        raw_operator_request="Review the current Airtable evaluation context.",
        specialist_task="Read the candidates and select the strongest current record.",
        decision_context={"success_criteria": "provider-bound selection"},
        provider_call_context={"provider": "airtable", "operation": "read"},
        side_effect_boundaries=["no_nested_live_write"],
    ).model_dump(mode="json")
    model = FakeModel(
        [
            [
                _tool_call(
                    case.tool_name,
                    child_input,
                    call_id="chief-parent-nested-context",
                )
            ],
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="chief-child-provider-read",
                )
            ],
            [_structured_message(_payload(case, decision=_decision(case)))],
            [_structured_message(_chief_parent_payload(valid_decision=False))],
            [_structured_message(_chief_parent_payload(valid_decision=True))],
        ]
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        requested_agent="chief_of_staff",
        target_agent="airtable_context_agent",
        workflow=["airtable_context_agent"],
        intent="context_lookup",
        primary_target="evaluation context",
        target_type="business_system_context",
        provider_system="airtable",
        provider_operations=["read"],
        task_objective="context_lookup",
    )
    chief = build_chief_of_staff_agent(
        include_specialist_tools=True,
        request_text=child_input["raw_operator_request"],
        manual_request_plan=plan,
    )

    result = run_typed_sdk_agent(
        agent=chief,
        typed_input=child_input["raw_operator_request"],
        output_type=ChiefOfStaffResult,
        run_config=build_local_run_config(FakeProvider(model)),
        decision_contract=chief_of_staff_decision_contract(),
    )

    nested_tool = next(
        item for item in chief.tools if getattr(item, "name", "") == case.tool_name
    )
    record = nested_tool.nested_execution_records["chief-parent-nested-context"]
    parent_decision = result.request_cache["decision_ownership"]
    assert provider_reads == 1
    assert len(model.calls) == 5
    assert case.tool_name in model.calls[0]["tool_names"]
    assert case.tool_name not in model.calls[3]["tool_names"]
    assert case.tool_name not in model.calls[4]["tool_names"]
    assert "Validated nested specialist evidence replay" not in model.calls[3][
        "system_instructions"
    ]
    assert "Validated nested specialist evidence replay" in model.calls[4][
        "system_instructions"
    ]
    assert "Selected the strongest current bounded context object" in model.calls[4][
        "system_instructions"
    ]
    assert case.actual_id not in model.calls[4]["system_instructions"]
    assert case.alternative_id not in model.calls[4]["system_instructions"]
    assert parent_decision["attempt_count"] == 2
    assert parent_decision["repair_attempted"] is True
    assert result.usage["requests"] == 3
    assert record["usage"]["requests"] == 2
    assert result.usage["requests"] + record["usage"]["requests"] == 5
    assert record["parent_repair_replay"]["status"] == "armed"
    assert record["parent_repair_replay"]["child_tool_disabled"] is True
    assert record["parent_repair_replay"]["injection_policy"] == (
        "fresh_parent_attempt_only"
    )
    assert record["parent_repair_replay"]["provider_calls_during_replay"] == 0
    assert record["parent_repair_replay"]["child_model_requests_during_replay"] == 0
    assert record["child_rerun_allowed"] is False


def test_chief_nested_replay_adapter_never_reruns_validated_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, "google_workspace_context_agent")
    provider_reads = 0
    provider_read = internal_data_tools.google_drive_search_files_impl

    def counted_provider_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal provider_reads
        provider_reads += 1
        return provider_read(*args, **kwargs)

    monkeypatch.setattr(
        internal_data_tools,
        "google_drive_search_files_impl",
        counted_provider_read,
    )
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="workspace-provider-read-once",
                )
            ],
            [_structured_message(_payload(case, decision=_decision(case)))],
        ]
    )
    chief = build_chief_of_staff_agent(include_specialist_tools=True)
    tool = next(item for item in chief.tools if getattr(item, "name", "") == case.tool_name)
    tool_input = ChiefSpecialistToolInput(
        raw_operator_request="Review current Workspace context.",
        specialist_task="Select the strongest current file.",
    ).model_dump(mode="json")
    serialized_input = json.dumps(tool_input)
    run_config = build_local_run_config(FakeProvider(model))

    def invoke(call_id: str) -> tuple[str, Any]:
        context = ToolContext(
            context=None,
            run_config=run_config,
            tool_name=tool.name,
            tool_call_id=call_id,
            tool_arguments=serialized_input,
        )
        output = asyncio.run(tool.on_invoke_tool(context, serialized_input))
        return output, context

    first_output, _first_context = invoke("workspace-first-child")
    replayed_output, replay_context = invoke("workspace-parent-repair-replay")

    assert replayed_output == first_output
    assert provider_reads == 1
    assert len(model.calls) == 2
    replay_record = replay_context._custom_data["keystone_nested_specialist_execution"]
    assert replay_record["execution_state"] == "replayed_validated_nested_specialist"
    assert replay_record["source_tool_call_id"] == "workspace-first-child"
    assert replay_record["provider_read_executed"] is False
    assert replay_record["usage"]["requests"] == 0
    assert replay_record["parent_repair_replay"]["provider_calls_during_replay"] == 0


def test_chief_nested_exhausted_repair_returns_blocked_not_child_prose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, "rss_context_agent")
    invalid = _payload(
        case,
        decision=_decision(case, stage="generic_selection"),
    )
    invalid["summary"] = "UNVERIFIED CHILD PROSE MUST NOT SURVIVE"
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    case.read_arguments,
                    call_id="rss-read",
                )
            ],
            [_structured_message(invalid)],
            [_structured_message(invalid)],
        ]
    )

    envelope, tool = _invoke_case(case, model)

    serialized = json.dumps(envelope)
    record = tool.nested_execution_records["chief-rss_context_agent-call"]
    assert envelope["parsed_output_status"] == "missing"
    assert envelope["validation_status"] == "blocked"
    assert "UNVERIFIED CHILD PROSE" not in serialized
    assert record["execution_state"] == "blocked"
    assert record["handoff"]["terminal_status"] == "blocked"
    assert record["reason_code"] == "structured_output_invalid"


def test_chief_nested_child_without_run_config_fails_closed() -> None:
    chief = build_chief_of_staff_agent(include_specialist_tools=True)
    tool = next(
        item
        for item in chief.tools
        if getattr(item, "name", "") == "airtable_context_agent_as_specialist_tool"
    )
    tool_input = ChiefSpecialistToolInput(
        raw_operator_request="Read one Airtable record without changing it.",
        specialist_task="Return the bounded record context.",
    ).model_dump(mode="json")
    context = ToolContext(
        context=None,
        run_config=None,
        tool_name=tool.name,
        tool_call_id="chief-missing-run-config",
        tool_arguments=json.dumps(tool_input),
    )

    envelope = json.loads(
        asyncio.run(tool.on_invoke_tool(context, json.dumps(tool_input)))
    )

    assert envelope["validation_status"] == "blocked"
    assert envelope["parsed_output_status"] == "missing"
    assert context._custom_data["keystone_nested_specialist_execution"][
        "reason_code"
    ] == "nested_run_config_missing"
    assert tool.nested_execution_records["chief-missing-run-config"][
        "reason_code"
    ] == "nested_run_config_missing"


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_chief_nested_live_parent_preserves_live_read_only_execution_mode(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    case = _configure_case(monkeypatch, route)
    observed: dict[str, Any] = {}
    if route == "airtable_context_agent":
        implementation = internal_data_tools.airtable_read_records_impl

        def capture_live(*args: Any, **kwargs: Any) -> dict[str, Any]:
            observed["live"] = kwargs.get("live")
            return implementation(*args, **kwargs)

        monkeypatch.setattr(
            internal_data_tools,
            "airtable_read_records_impl",
            capture_live,
        )
    elif route == "google_workspace_context_agent":
        implementation = internal_data_tools.google_drive_search_files_impl

        def capture_live(*args: Any, **kwargs: Any) -> dict[str, Any]:
            observed["live"] = kwargs.get("live")
            return implementation(*args, **kwargs)

        monkeypatch.setattr(
            internal_data_tools,
            "google_drive_search_files_impl",
            capture_live,
        )
    else:
        implementation = zotero_context_tools._read_zotero_api_json

        def capture_zotero_provider_read(*args: Any, **kwargs: Any) -> Any:
            observed["provider_read"] = True
            return implementation(*args, **kwargs)

        monkeypatch.setattr(
            zotero_context_tools,
            "_read_zotero_api_json",
            capture_zotero_provider_read,
        )
    model = FakeModel(
        [
            [
                _tool_call(
                    case.read_tool_name,
                    {**case.read_arguments, "live": False},
                    call_id="airtable-live-read",
                )
            ],
            [_structured_message(_payload(case, decision=_decision(case)))],
        ]
    )

    envelope, tool = _invoke_case(case, model, nested_live_execution=True)

    diagnostics = {item["key"]: item["value"] for item in envelope["diagnostics"]}
    record = tool.nested_execution_records[f"chief-{route}-call"]
    assert "live provider mode" in json.dumps(model.calls[0]["input"], default=str)
    assert not any(
        operation_is_mutation(tool_name)
        for tool_name in model.calls[0]["tool_names"]
    )
    assert diagnostics["nested_execution_mode"] == "live_read_only"
    assert record["nested_execution_mode"] == "live_read_only"
    assert record["provider_write_executed"] is False
    if route == "zotero_context_agent":
        assert observed["provider_read"] is True
    else:
        assert observed["live"] is True
    assert record["nested_live_read_enforcements"] == [
        {
            "schema": "keystone.nested_live_read_enforcement_trace.v1",
            "origin": "sdk_tool_call_output_custom_data",
            "custom_data_key": "keystone_nested_live_read_enforcement",
            "tool_name": case.read_tool_name,
            "requested_live": False,
            "effective_live": True,
            "enforced": True,
            "read_only": True,
            "mutation_capability_enabled": False,
            "raw_sensitive_values_retained": False,
        }
    ]
    assert "keystone_nested_live_read_enforcement" not in json.dumps(envelope)
