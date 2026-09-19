"""Selected provider Gmail evidence reaches the next real SDK input without rereads."""

from __future__ import annotations

import base64
import json

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from keystone_agents import workflow_runner as workflow
from keystone_agents.agents.gmail_triage import run_gmail_triage_sdk
from keystone_agents.gmail_triage.handoff_context import (
    gmail_context_source_refs,
    selected_provider_context,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.gmail_query import GmailReadContextResult
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import gmail_query_tools
from keystone_agents.tools.gmail_tool import GmailTool


def message(identity="selected"):
    return {
        "id": f"msg-{identity}",
        "threadId": f"thread-{identity}",
        "received_at": "2026-09-01T12:00:00Z",
        "sender_name": "Newsletter Publisher",
        "sender_email": "editor@publisher.example.test",
        "subject": "Company newsletter",
        "snippet": f"{identity} bounded snippet",
        "prior_labels": [],
    }


def projection(identity="selected"):
    return {
        **message(identity),
        "thread_summary": f"{identity} provider summary.",
        "thread_context": (
            "SELECTED_PROVIDER_EVIDENCE Featured Company announced an evaluation pilot."
            if identity == "selected"
            else "UNSELECTED_PROVIDER_EVIDENCE"
        ),
        "body": "FULL_PROVIDER_BODY_MUST_NOT_PROPAGATE",
        "extracted_links": [{"url": f"https://example.test/{identity}-article"}],
        "triage_limitations": ["Provider returned a bounded excerpt; full body omitted."],
    }


def triage_output():
    return EmailTriageResult(
        message_id="msg-selected",
        thread_id="thread-selected",
        subject="Company newsletter",
        sender_name="Newsletter Publisher",
        sender_email="editor@publisher.example.test",
        received_at="2026-09-01T12:00:00Z",
        category="newsletter",
        confidence=0.9,
        summary="Selected newsletter reviewed.",
        reasoning="The selected conversation contains the requested announcement.",
        recommended_action="Research the requested company.",
        recommended_next_agent="business_research_analyst",
        normalized_body="MODEL_AUTHORED_BODY_MUST_NOT_BECOME_EVIDENCE",
        extracted_links=[{"url": "https://example.test/model-invented"}],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "gmail_candidate_selection",
            "selected_candidate_id": "thread-selected",
            "needs_more_context": False,
            "reasoning": "Selected from the exact provider contexts read in this run.",
            "candidate_assessments": [
                {
                    "candidate_id": f"thread-{identity}",
                    "disposition": disposition,
                    "rationale": "The requested conversation."
                    if identity == "selected"
                    else "The other conversation is outside this request.",
                }
                for identity, disposition in (("selected", "selected"), ("other", "excluded"))
            ],
        },
    )


class GmailModel(Model):
    def __init__(self, *, schema_retry=False):
        self.calls = 0
        self.schema_retry = schema_retry

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    name="query_gmail_message_summaries",
                    call_id="query-selected-context",
                    arguments=json.dumps(
                        {
                            "query": "newsletter",
                            "label": "INBOX",
                            "max_results": 2,
                        }
                    ),
                )
            ]
        elif self.calls == 2:
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    name="read_gmail_context",
                    call_id=f"read-{identity}",
                    arguments=json.dumps(
                        {
                            "resource_type": "message",
                            "resource_id": f"msg-{identity}",
                        }
                    ),
                )
                for identity in ("selected", "other")
            ]
        else:
            output = [
                ResponseOutputMessage(
                    id="selected-final",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=(
                                "{"
                                if self.schema_retry and self.calls == 3
                                else triage_output().model_dump_json()
                            ),
                            annotations=[],
                        )
                    ],
                )
            ]
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=80, output_tokens=30),
            response_id=f"gmail-evidence-{self.calls}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def test_sdk_recovers_after_two_empty_queries_without_selection_repair(monkeypatch):
    """The real SDK loop consumes every search result before its next decision."""
    queries = (
        "annual regional technology planning roundtable next year",
        "regional planning roundtable",
        "subject:roundtable",
    )
    provider_queries = []
    provider_reads = []
    contexts = []
    candidate = {**message(), "subject": "Planning roundtable invitation"}

    def search(**kwargs):
        provider_queries.append(kwargs["query"])
        return [candidate] if kwargs["query"] == queries[2] else []

    def read(identity):
        provider_reads.append(identity)
        return {**projection(), "subject": candidate["subject"]}

    class RecoveryModel(GmailModel):
        async def get_response(self, *args, **kwargs):
            self.calls += 1
            assert self.calls <= 5, "The accepted selection must not trigger another model call"
            if self.calls > 1:
                previous = next(item for item in reversed(kwargs["input"])
                                if item.get("type") == "function_call_output")
                evidence = json.loads(previous["output"])
                assert evidence["provider_read_performed"] is True
                if self.calls <= 4:
                    assert evidence["remaining_query_calls"] == 4 - self.calls
                    assert evidence["item_count"] == int(self.calls == 4)
                else:
                    assert evidence["resource_id"] == candidate["id"]
                    assert "SELECTED_PROVIDER_EVIDENCE" in evidence["thread_context"]
            if self.calls <= 3:
                output = [ResponseFunctionToolCall(
                    type="function_call", name="query_gmail_message_summaries",
                    call_id=f"query-{self.calls}",
                    arguments=json.dumps({
                        "query": queries[self.calls - 1], "label": "", "max_results": 4,
                    }),
                )]
            elif self.calls == 4:
                output = [ResponseFunctionToolCall(
                    type="function_call", name="read_gmail_context", call_id="read-selected",
                    arguments=json.dumps({
                        "resource_type": "message",
                        "resource_id": evidence["items"][0]["message_id"],
                    }),
                )]
            else:
                selected = triage_output()
                selected = selected.model_copy(update={
                    "subject": evidence["subject"],
                    "decision": selected.decision.model_copy(update={
                        "candidate_assessments": selected.decision.candidate_assessments[:1],
                    }),
                })
                output = [ResponseOutputMessage(
                    id="recovered-final", type="message", role="assistant", status="completed",
                    content=[ResponseOutputText(
                        type="output_text", text=selected.model_dump_json(), annotations=[],
                    )],
                )]
            return ModelResponse(
                output=output, usage=Usage(requests=1, input_tokens=80, output_tokens=30),
                response_id=f"recovery-{self.calls}",
            )

    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "search_message_summaries", search)
    monkeypatch.setattr(gmail_query_tools.gmail_tool, "get_message_context_projection", read)
    model = RecoveryModel()
    result = run_gmail_triage_sdk(
        "Find the planning roundtable invitation in Gmail. Read it and summarize it; no changes.",
        run_config=build_local_run_config(Provider(model)), live=True,
        provider_selection_required=True, selected_context_callback=contexts.append,
    )
    assert model.calls == 5
    assert provider_queries == list(queries)
    assert provider_reads == [candidate["id"]]
    assert result.output.message_id == candidate["id"]
    assert result.output.thread_id == candidate["threadId"]
    assert result.output.subject == candidate["subject"]
    assert len(contexts) == 1 and contexts[0].resource_id == candidate["id"]
    assert result.request_cache.get("decision_repairs", 0) == 0
    ownership = result.request_cache["decision_ownership"]
    assert ownership["query_call_count"] == ownership["query_output_count"] == 3
    assert ownership["corrective_query_count"] == 2
    assert ownership["context_read_call_count"] == 1
    assert ownership["validator_outcome"]["status"] == "accepted"
    assert ownership["validator_outcome"]["repair_attempted"] is False


def test_actual_sdk_follows_long_gmail_windows_and_persists_late_evidence(
    tmp_path,
    monkeypatch,
):
    qualification = "FINAL CONDITION: reply only if independent review is complete."
    source_body = "Newsletter detail. " * 700 + qualification
    raw = {
        "id": "msg-selected",
        "threadId": "thread-selected",
        "historyId": "history-long-sdk",
        "internalDate": "1789600000000",
        "snippet": "Newsletter detail.",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Company newsletter"},
                {
                    "name": "From",
                    "value": "Newsletter Publisher <editor@publisher.example.test>",
                },
            ],
            "body": {"data": base64.urlsafe_b64encode(source_body.encode()).decode()},
        },
    }
    provider_tool = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(provider_tool, "_request", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(
        provider_tool,
        "current_account_email",
        lambda: "reader@example.test",
    )
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **_kwargs: [message()],
    )
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "get_message_context_projection",
        provider_tool.get_message_context_projection,
    )

    class LongSourceModel(GmailModel):
        def __init__(self):
            super().__init__()
            self.model_inputs = []

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            self.model_inputs.append(kwargs["input"])
            previous = None
            if self.calls > 1:
                previous = json.loads(
                    next(
                        item["output"]
                        for item in reversed(kwargs["input"])
                        if item.get("type") == "function_call_output"
                    )
                )
            if self.calls == 1:
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="query_gmail_message_summaries",
                        call_id="query-long-source",
                        arguments=json.dumps(
                            {"query": "company newsletter", "label": "", "max_results": 2}
                        ),
                    )
                ]
            elif self.calls == 2:
                assert previous is not None and previous["item_count"] == 1
                output = [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="read_gmail_context",
                        call_id="read-long-source-0",
                        arguments=json.dumps(
                            {
                                "resource_type": "message",
                                "resource_id": "msg-selected",
                            }
                        ),
                    )
                ]
            elif self.calls >= 3:
                assert previous is not None
                evidence = previous["body_evidence"][0]
                next_request = evidence.get("next_request")
                if next_request is not None:
                    assert self.calls <= 10
                    output = [
                        ResponseFunctionToolCall(
                            type="function_call",
                            name="read_gmail_context",
                            call_id=f"read-long-source-{self.calls - 2}",
                            arguments=json.dumps(next_request),
                        )
                    ]
                else:
                    assert qualification in evidence["source_text"]
                    selected = triage_output().model_copy(
                        update={
                            "summary": qualification,
                            "reasoning": (
                                "The late condition in the exact selected source controls."
                            ),
                            "decision": triage_output().decision.model_copy(
                                update={
                                    "candidate_assessments": (
                                        triage_output().decision.candidate_assessments[:1]
                                    )
                                }
                            ),
                        }
                    )
                    output = [
                        ResponseOutputMessage(
                            id="long-source-final",
                            type="message",
                            role="assistant",
                            status="completed",
                            content=[
                                ResponseOutputText(
                                    type="output_text",
                                    text=selected.model_dump_json(),
                                    annotations=[],
                                )
                            ],
                        )
                    ]
            return ModelResponse(
                output=output,
                usage=Usage(requests=1, input_tokens=80, output_tokens=30),
                response_id=f"gmail-long-source-{self.calls}",
            )

    model = LongSourceModel()
    contexts = []
    result = run_gmail_triage_sdk(
        "What condition in the selected company newsletter controls whether I should reply?",
        run_config=build_local_run_config(Provider(model)),
        live=True,
        provider_selection_required=True,
        selected_context_callback=contexts.append,
    )

    assert result.output.summary == qualification
    assert model.calls == ((len(source_body) + 2_999) // 3_000) + 2
    assert qualification in json.dumps(model.model_inputs[-1])
    assert len(contexts) == 1
    merged = contexts[0]
    assert qualification in "".join(item.source_text for item in merged.body_evidence)
    refs = gmail_context_source_refs(merged)
    assert qualification in "".join(page.evidence_excerpt for page in refs[0].evidence_pages)
    database_url = f"sqlite:///{tmp_path / 'gmail-long-source.db'}"
    store = SQLiteStore(database_url)
    parent_request = (
        "What condition in the selected company newsletter controls whether I should reply?"
    )
    work_item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Long Gmail source",
        request_text=parent_request,
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Company newsletter", object_type="gmail_thread"),
    )
    store.save_work_item(work_item)

    def parent_sdk_boundary(_typed_input, **kwargs):
        kwargs["selected_context_callback"](merged)
        return result

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", parent_sdk_boundary)
    parent_result = workflow._try_live_gmail_agent_owned_triage(
        work_item,
        request=WorkflowRunRequest(request_text=parent_request, live_sdk=True),
        store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(parent_request),
    )

    assert parent_result.status.value == "in_progress"
    assert parent_result.next_action is not None
    assert qualification in "".join(
        page.evidence_excerpt for page in parent_result.work_item.sources[0].evidence_pages
    )
    store.save_work_item(parent_result.work_item)
    restored = SQLiteStore(database_url).get_work_item(work_item.id)
    assert restored is not None
    assert qualification in "".join(
        page.evidence_excerpt for page in restored.sources[0].evidence_pages
    )
    assert restored.sources[0].url == merged.source_url


def test_selected_thread_timeline_survives_persistence_and_outreach_loading(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from keystone_agents.models import TypedAgentRunResult

    timeline = [{
        "message_id": f"message-{i}", "thread_id": "thread-selected",
        "received_at": f"2026-01-{i + 1:02d}T12:00:00Z",
        "sender_email": "editor@example.test" if i == 0 else "reader@example.test",
        "subject": "Correspondence", "snippet": "Welcome" if i == 0 else "Following up",
        "prior_labels": ["INBOX"] if i == 0 else ["SENT"],
    } for i in range(3)]
    context = GmailReadContextResult(
        status="read", provider_read_performed=True,
        resource_type="thread", resource_id="thread-selected",
        messages=timeline, message_count=3, subject="Correspondence",
        summary="One incoming message followed by two outbound follow-ups.",
        source_url="https://mail.google.com/mail/?authuser=reader%40example.test#all/thread-selected",
        triage_limitations=["Only this selected conversation was inspected."],
    )
    selected = triage_output().model_copy(update={
        "message_id": "", "subject": context.subject, "recommended_next_agent": "human_review",
    })

    def sdk_boundary(_typed_input, **kwargs):
        kwargs["selected_context_callback"](context)
        return TypedAgentRunResult(
            agent_name="gmail_triage", output=selected, raw_result=SimpleNamespace(new_items=[]),
            live=True,
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", sdk_boundary)
    database_url = f"sqlite:///{tmp_path / 'thread-timeline.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD, title="Correspondence review",
        request_text="Review the correspondence chronology; do not send anything.",
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Correspondence", object_type="gmail_thread"),
    )
    store.save_work_item(item)
    request = WorkflowRunRequest(
        request_text=item.request_text, work_item_id=item.id,
        live_sdk=True, save=True, database_url=database_url, sdk_session_enabled=False,
    )
    result = workflow._try_live_gmail_agent_owned_triage(
        item, request=request, store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(item.request_text),
    )
    assert result.advanced
    restored = workflow._latest_gmail_thread_summary_for_outreach(
        result.work_item, store=SQLiteStore(database_url),
    )
    assert restored.source_label == "gmail_triage_sdk_selected"
    assert restored.message_count == 3
    assert len(restored.messages) == 3
    assert restored.triage_limitations == context.triage_limitations
    for expected, actual in zip(context.messages, restored.messages, strict=True):
        assert actual.message_id == expected.message_id
        assert actual.received_at == expected.received_at
        assert actual.sender_email == expected.sender_email
        assert actual.prior_labels == expected.prior_labels
        assert actual.snippet == expected.snippet
    assert any(source.url == context.source_url for source in result.work_item.sources)


@pytest.mark.parametrize("schema_retry", [False, True])
def test_selected_read_survives_workitem_and_actual_research_input(
    tmp_path,
    monkeypatch,
    supplied_research_fake_sdk,
    schema_retry,
):
    provider_reads = []
    callbacks = []
    if schema_retry:
        # Injected local RunConfig normally disables provider retries; exercise the
        # production one-repair policy with the same network-free scripted Model.
        monkeypatch.setattr("keystone_agents.run._sdk_structured_output_max_retries",
                            lambda **kwargs: 1)
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setattr(
        gmail_query_tools.gmail_tool,
        "search_message_summaries",
        lambda **kwargs: [message(), message("other")],
    )

    def read(identity):
        provider_reads.append(identity)
        return projection(identity.removeprefix("msg-"))

    monkeypatch.setattr(gmail_query_tools.gmail_tool, "get_message_context_projection", read)
    model = GmailModel(schema_retry=schema_retry)

    def local_sdk(typed_input, **kwargs):
        callback = kwargs["selected_context_callback"]

        def selected(context):
            callbacks.append(context)
            callback(context)

        return run_gmail_triage_sdk(
            typed_input,
            **{
                **kwargs,
                "selected_context_callback": selected,
                "run_config": build_local_run_config(Provider(model)),
            },
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", local_sdk)
    raw_request = (
        "Review the relevant newsletter, then research Featured Company from that evidence."
    )
    plan = {
        "source": "orchestrator_canonical",
        "target_agent": "gmail_triage",
        "intent": "gmail_triage",
        "workflow": ["gmail_triage", "business_research_analyst"],
        "primary_target": "Featured Company",
        "target_type": "company",
        "provider_system": "gmail",
        "provider_operations": ["read", "search"],
    }
    database_url = f"sqlite:///{tmp_path / 'gmail-research.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Newsletter research",
        request_text=raw_request,
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(
            name="Featured Company", object_type="company", metadata={"manual_request_plan": plan}
        ),
    )
    request = WorkflowRunRequest(
        request_text=raw_request,
        work_item_id=item.id,
        live_sdk=True,
        live_search=False,
        save=True,
        database_url=database_url,
        sdk_session_enabled=False,
        manual_request_plan=plan,
    )
    store.save_work_item(item)
    result = workflow._try_live_gmail_agent_owned_triage(
        item,
        request=request,
        store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(raw_request, manual_plan=plan),
    )
    assert result.advanced, result.audit_notes[0] if result.audit_notes else result.blockers
    assert model.calls == (4 if schema_retry else 3)
    usage_events = [event for event in store.list_work_item_events(item.id)
                    if event.event_type == "workflow_sdk_usage"]
    assert sum(event.metadata["usage"]["requests"] for event in usage_events) == model.calls
    assert sum(event.metadata["usage"]["input_tokens"] for event in usage_events) == (
        80 * model.calls
    )
    assert result.tool_execution["postcondition"]["satisfied"] is True
    assert sorted(provider_reads) == ["msg-other", "msg-selected"]
    assert len(callbacks) == 1 and callbacks[0].resource_id == "msg-selected"
    workflow.finalize_prepared_work_item_step(
        workflow.PreparedWorkItemStep(
            request=request,
            work_item=item,
            route=WorkItemRoute.GMAIL_TRIAGE,
            input_text=raw_request,
            context_pack={},
        ),
        result,
        synthesize_user_response=False,
    )
    restored = SQLiteStore(database_url).get_work_item(item.id)
    assert restored.target.name == "Featured Company"
    serialized = restored.model_dump_json()
    assert "SELECTED_PROVIDER_EVIDENCE" in serialized
    assert "https://example.test/selected-article" in serialized
    assert "thread-selected/message/msg-selected" in serialized
    assert "full body omitted" in serialized
    for event in store.list_work_item_events(item.id):
        if event.event_type == "workflow_sdk_usage":
            assert "SELECTED_PROVIDER_EVIDENCE" not in json.dumps(event.metadata)
    for excluded in (
        "UNSELECTED_PROVIDER_EVIDENCE",
        "FULL_PROVIDER_BODY_MUST_NOT_PROPAGATE",
        "MODEL_AUTHORED_BODY_MUST_NOT_BECOME_EVIDENCE",
        "model-invented",
    ):
        assert excluded not in serialized
    # The next drafting stage must consume the exact read, not model-written body text.
    provider_context = workflow._latest_gmail_thread_summary_for_outreach(restored, store=store)
    assert provider_context.source_label == "gmail_triage_sdk_selected"
    assert provider_context.thread_id == "thread-selected"
    assert "SELECTED_PROVIDER_EVIDENCE" in provider_context.thread_context
    assert "MODEL_AUTHORED_BODY_MUST_NOT_BECOME_EVIDENCE" not in provider_context.model_dump_json()
    assert "UNSELECTED_PROVIDER_EVIDENCE" not in provider_context.model_dump_json()
    from keystone_agents.schemas.work_item import WorkItemArtifactRef
    other_id = store.save_agent_run(
        agent_name="gmail_triage", input_payload={}, input_summary="Unselected result",
        output={"thread_id": "other", "subject": "Other", "summary": "Not selected"},
        model="fixture", dry_run=True,
    )
    with_unselected = restored.model_copy(update={"artifact_refs": [
        *restored.artifact_refs, WorkItemArtifactRef(artifact_type="gmail_triage_report",
            artifact_id=str(other_id), selected=False),
    ]})
    assert workflow._latest_gmail_thread_summary_for_outreach(
        with_unselected, store=store,
    ) == provider_context
    research = workflow._advance_research(restored, request=request, store=store)
    assert research.advanced, research.blockers
    assert len(supplied_research_fake_sdk) == 1
    model_input = supplied_research_fake_sdk[0]["model_input"]
    assert raw_request in model_input
    assert "SELECTED_PROVIDER_EVIDENCE" in model_input
    assert "https://example.test/selected-article" in model_input
    assert "page contents have not been retrieved or verified" in model_input
    assert "full body omitted" in model_input
    assert "UNSELECTED_PROVIDER_EVIDENCE" not in model_input
    assert sorted(provider_reads) == ["msg-other", "msg-selected"]


@pytest.mark.parametrize("change", ["wrong_identity", "fixture", "not_found", "model_only"])
def test_unverified_or_unselected_context_is_never_promoted(change):
    context = gmail_query_tools._message_context_from_provider("msg-selected", projection())
    output = triage_output()
    entry = {"tool_name": "read_gmail_context", "output": context.model_dump(mode="json")}
    if change == "wrong_identity":
        output = output.model_copy(update={"message_id": "msg-other", "thread_id": "thread-other"})
    elif change == "fixture":
        entry["output"]["provider_read_performed"] = False
    elif change == "not_found":
        entry["output"]["status"] = "not_found"
    else:
        entry["tool_name"] = "model_final_output"
    assert selected_provider_context(output, [entry]) is None


def test_selected_context_uses_cumulative_ledger_and_preserves_link_limits():
    values = [
        {"url": "https://example.test/article"},
        {"url": "file:///private/document"},
        {"url": "https://name:password@example.test/private"},
        {"url": "https://example.test/suspicious", "suspicious": True},
        {"url": "https://example.test/" + "x" * 2050},
    ]
    context = gmail_query_tools._message_context_from_provider(
        "msg-selected",
        {**projection(), "extracted_links": values},
    )
    snapshot = [{"tool_name": "read_gmail_context", "output": context.model_dump(mode="json")}]
    selected = selected_provider_context(triage_output(), snapshot)
    assert selected is not None
    assert [link.url for link in selected.extracted_links] == [
        "https://example.test/article",
        "https://example.test/suspicious",
    ]
    refs = gmail_context_source_refs(selected)
    assert [ref.url for ref in refs[1:]] == ["https://example.test/article"]
    bounded = GmailReadContextResult.model_validate(
        {
            **context.model_dump(),
            "extracted_links": [{"url": f"https://example.test/{i}"} for i in range(30)],
        }
    )
    assert len(bounded.extracted_links) == 10


def test_selected_context_retains_more_than_sixteen_windows_as_bounded_pages():
    snapshot = "a" * 64
    entries = []
    for index in range(17):
        start = index * 3_000
        end = start + 3_000
        context = GmailReadContextResult.model_validate(
            {
                "status": "read",
                "resource_type": "message",
                "resource_id": "msg-selected",
                "thread_id": "thread-selected",
                "account_identity_sha256": "b" * 64,
                "source_snapshot_sha256": snapshot,
                "message": {
                    "message_id": "msg-selected",
                    "thread_id": "thread-selected",
                    "received_at": "2026-09-01T12:00:00Z",
                    "sender_name": "Newsletter Publisher",
                    "sender_email": "editor@publisher.example.test",
                    "subject": "Company newsletter",
                    "snippet": "selected bounded snippet",
                    "prior_labels": [],
                },
                "message_count": 1,
                "subject": "Company newsletter",
                "summary": "Bounded selected source.",
                "thread_context": "EARLY CONDITION",
                "source_url": (
                    "https://mail.google.com/mail/?authuser=reader%40example.test"
                    "#all/thread-selected"
                ),
                "body_evidence": [
                    {
                        "message_id": "msg-selected",
                        "thread_id": "thread-selected",
                        "part_path": "0",
                        "mime_type": "text/plain",
                        "representation": "plain",
                        "role": "single_representation",
                        "source_text": (
                            "EARLY WINDOW"
                            if index == 0
                            else "LATE WINDOW"
                            if index == 16
                            else f"window-{index}"
                        ).ljust(3000),
                        "content_complete": index == 16,
                        "truncated": index != 16,
                        "coverage": {
                            "start_char": start,
                            "end_char": end,
                            "full_char_count": 51_000,
                            "complete": index == 16,
                            "has_more": index != 16,
                        },
                    }
                ],
                "body_content_status": "complete" if index == 16 else "partial",
                "body_content_complete": index == 16,
                "provider_read_performed": True,
            }
        )
        entries.append(
            {"tool_name": "read_gmail_context", "output": context.model_dump(mode="json")}
        )

    output = triage_output().model_copy(
        update={
            "decision": triage_output().decision.model_copy(
                update={
                    "candidate_assessments": (
                        triage_output().decision.candidate_assessments[:1]
                    )
                }
            )
        }
    )
    selected = selected_provider_context(output, entries)

    assert selected is not None
    assert len(selected.body_evidence) == 16
    assert selected.body_evidence[0].source_text.startswith("EARLY WINDOW")
    windows = [window for page in selected.source_evidence_pages for window in page.windows]
    assert len(windows) == selected.source_windows_read == selected.source_windows_retained == 17
    assert windows[-1].source_text.startswith("LATE WINDOW")
    assert selected.source_evidence_complete is True
    assert selected.body_content_complete is False
    assert selected.body_content_status == "partial"
    assert any("all windows are retained" in item for item in selected.triage_limitations)
    key_facts = gmail_context_source_refs(selected)[0].key_facts
    assert "body_content_complete=false" in key_facts
    assert "source_windows_read=17" in key_facts
    assert "source_windows_retained=17" in key_facts


def test_bounded_message_adapter_retains_actual_html_article_url(monkeypatch):
    html = (
        "<p>Newsletter excerpt.</p>" + "\u200c" * 2500
        + "<p>" + "Context detail. " * 120 + "DEEP_SELECTED_BODY_EVIDENCE</p>"
        + '<a href="https://example.test/exact-article">Read article</a>'
    )
    api_message = {
        "id": "msg-selected",
        "threadId": "thread-selected",
        "snippet": "Newsletter excerpt.",
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "From", "value": "Publisher <editor@publisher.example.test>"},
                {"name": "Subject", "value": "Company newsletter"},
            ],
            "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
        },
    }
    client = object.__new__(GmailTool)
    client.live = True
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ("GET", "profile"):
            return {"emailAddress": "reader@example.test"}
        return api_message

    monkeypatch.setattr(client, "_request", request)
    value = client.get_message_context_projection("msg-selected")
    context = gmail_query_tools._message_context_from_provider("msg-selected", value)
    assert len(calls) == 2
    assert "authuser=reader%40example.test#all/thread-selected" in context.source_url
    assert "Newsletter excerpt." in context.thread_context
    assert "DEEP_SELECTED_BODY_EVIDENCE" in context.thread_context
    assert "\u200c" not in context.thread_context
    assert gmail_context_source_refs(context)[0].url == context.source_url
    assert [link.url for link in context.extracted_links] == ["https://example.test/exact-article"]
    assert "body" not in value and "normalized_body" not in value
    assert context.raw_message_bodies_returned is False


def test_thread_projection_retains_only_bounded_safe_provider_links():
    value = {
        "thread_id": "thread-selected",
        "message_count": 2,
        "summary": "Bounded thread summary.",
        "thread_context": "Bounded thread excerpt.",
        "messages": [
            {"extracted_links": [{"url": "https://example.test/article"}]},
            {"extracted_links": [{"url": "https://example.test/second-article"}]},
        ],
    }
    context = gmail_query_tools._thread_context_from_provider("thread-selected", value)
    assert [link.url for link in context.extracted_links] == [
        "https://example.test/article",
        "https://example.test/second-article",
    ]
    selected = selected_provider_context(
        triage_output(),
        [
            {
                "tool_name": "read_gmail_context",
                "output": context.model_dump(mode="json"),
            }
        ],
    )
    assert selected is not None and selected.resource_id == "thread-selected"
    assert selected.raw_message_bodies_returned is False
