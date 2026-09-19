"""Retained Gmail pages survive actual SDK tools and persisted WorkItem follow-ups."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import socket

import pytest
from agents.models.interface import ModelResponse
from agents.tool_context import ToolContext
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from test_gmail_research_evidence_handoff import GmailModel, Provider, message, triage_output

from keystone_agents import workflow_runner as workflow
from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.agents.gmail_triage import run_gmail_triage_sdk
from keystone_agents.gmail_triage.handoff_context import (
    gmail_context_source_refs,
    selected_provider_context,
)
from keystone_agents.langgraph_workflow import langgraph_available
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import gmail_query_tools as query
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.work_item_source_tool import build_work_item_source_evidence_tool
from keystone_agents.work_items import build_research_context_pack

MARKERS = (
    "EARLY: external use requires permission.",
    "MIDDLE: budget is exactly £0; no reimbursement.",
    "LATE: NOT approved until independent verification.",
)


def install_provider(monkeypatch, repeats=2800):
    body = (
        MARKERS[0]
        + " Context. "
        + "Context. " * repeats
        + MARKERS[1]
        + " Context. "
        + "Context. " * repeats
        + MARKERS[2]
    )
    raw = {
        "id": "msg-selected",
        "threadId": "thread-selected",
        "historyId": "10",
        "internalDate": "1700000000000",
        "snippet": "Conditions",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Conditions"},
                {"name": "From", "value": "author@example.test"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    }
    provider = GmailTool(live=True, access_token="synthetic-unused-token")
    monkeypatch.setattr(provider, "_request", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(provider, "current_account_email", lambda: "reader@example.test")
    monkeypatch.setattr(query.gmail_tool, "_gmail_read_tool", lambda **_kwargs: provider)
    monkeypatch.setattr(query.gmail_tool, "search_message_summaries", lambda **_kwargs: [message()])
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_SDK_MAX_TURNS", "40")
    monkeypatch.setattr(socket.socket, "connect", lambda *_args: pytest.fail("Network forbidden"))
    return body, raw


def read_entries():
    entries = []
    request = {"resource_type": "message", "resource_id": "msg-selected"}
    for _ in range(40):
        output = query.read_gmail_context_impl(live=True, **request).model_dump(mode="json")
        entries.append({"tool_name": "read_gmail_context", "arguments": request, "output": output})
        request = output["body_evidence"][0].get("next_request")
        if request is None:
            return entries
    pytest.fail("Source continuation did not terminate")


def selection():
    result = triage_output()
    return result.model_copy(
        update={
            "decision": result.decision.model_copy(
                update={
                    "candidate_assessments": result.decision.candidate_assessments[:1],
                }
            )
        }
    )


def invoke(tool, **kwargs):
    if callable(tool):
        return json.loads(tool(**kwargs))
    return json.loads(
        asyncio.run(
            tool.on_invoke_tool(
                ToolContext(
                    context=None,
                    tool_name=tool.name,
                    tool_call_id="test-read",
                    tool_arguments=json.dumps(kwargs),
                ),
                json.dumps(kwargs),
            )
        )
    )


@pytest.mark.parametrize("repeats", [1400, 2800])
def test_all_windows_retained_and_recoverable_after_sqlite_reload(monkeypatch, tmp_path, repeats):
    body, _ = install_provider(monkeypatch, repeats)
    entries = read_entries()
    selected = selected_provider_context(selection(), entries)
    assert selected is not None
    assert selected.source_windows_read == selected.source_windows_retained == len(entries)
    assert selected.source_evidence_complete is True
    assert len(selected.body_evidence) <= 16
    ref = gmail_context_source_refs(selected)[0]
    assert MARKERS[1] not in ref.evidence_excerpt
    assert "".join(page.evidence_excerpt for page in ref.evidence_pages) == body
    assert ref.evidence_access.page_count == len(entries)
    url = f"sqlite:///{tmp_path / 'retained.db'}"
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Conditions",
        request_text="Review conditions",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Conditions", object_type="topic"),
        sources=[ref],
    )
    SQLiteStore(url).save_work_item(item)
    restored = SQLiteStore(url).get_work_item(item.id)
    pack = build_research_context_pack(restored)
    assert MARKERS[1] not in pack.model_dump_json()
    assert pack.source_refs[0].evidence_access == ref.evidence_access
    tool = build_work_item_source_evidence_tool(restored.sources)
    request = dict(
        source_id=ref.source_id, expected_snapshot_sha256=ref.evidence_access.snapshot_sha256
    )
    recovered = []
    while request is not None:
        result = invoke(tool, **request)
        assert result["status"] == "read"
        assert result["provider_read_performed"] is False
        assert result["provider_write_performed"] is False
        assert len(result["pages"]) <= 3
        recovered.extend(result["pages"])
        request = result["next_request"]
    assert "".join(page["evidence_excerpt"] for page in recovered) == body
    for index, page in enumerate(recovered):
        provenance = json.loads(page["provenance_json"])
        assert provenance["message_id"] == "msg-selected"
        assert provenance["thread_id"] == "thread-selected"
        assert provenance["coverage"]["start_char"] == index * 3000
        assert provenance["source_snapshot_sha256"] == selected.source_snapshot_sha256
    for arguments, status in [
        (
            {
                "source_id": "unselected",
                "expected_snapshot_sha256": ref.evidence_access.snapshot_sha256,
            },
            "not_found",
        ),
        ({"source_id": ref.source_id, "expected_snapshot_sha256": "0" * 64}, "source_changed"),
        (
            {
                "source_id": ref.source_id,
                "expected_snapshot_sha256": ref.evidence_access.snapshot_sha256,
                "page_index": -1,
            },
            "out_of_range",
        ),
    ]:
        rejected = invoke(tool, **arguments)
        assert rejected["status"] == status and "pages" not in rejected


@pytest.mark.parametrize(
    "corruption", ["account", "snapshot", "nested_identity", "coverage", "overlap", "changed"]
)
def test_handoff_rejects_mixed_or_unverifiable_windows(monkeypatch, corruption):
    install_provider(monkeypatch, repeats=400)
    entries = read_entries()
    payload = entries[-1]["output"]
    if corruption == "account":
        payload["account_identity_sha256"] = "0" * 64
    elif corruption == "snapshot":
        payload["source_snapshot_sha256"] = "0" * 64
    elif corruption == "nested_identity":
        payload["body_evidence"][0]["message_id"] = "other-message"
    elif corruption == "coverage":
        payload["body_evidence"][0]["coverage"]["end_char"] -= 1
    elif corruption == "overlap":
        duplicate = copy.deepcopy(entries[0])
        text = duplicate["output"]["body_evidence"][0]["source_text"]
        duplicate["output"]["body_evidence"][0]["source_text"] = "X" + text[1:]
        entries.append(duplicate)
    else:
        payload.update(status="source_changed", source_restart_required=True)
    assert selected_provider_context(selection(), entries) is None


@pytest.mark.parametrize("graph", [
    False,
    pytest.param(
        True,
        marks=pytest.mark.skipif(
            not langgraph_available(), reason="optional LangGraph is not installed"
        ),
    ),
])
def test_actual_sdk_retains_and_reads_long_source_after_parent_workitem_reload(
    monkeypatch, tmp_path, graph
):
    body, _ = install_provider(monkeypatch)
    gmail_inputs = []
    contexts = []

    def response(output, count):
        return ModelResponse(
            output=output,
            usage=Usage(requests=1, input_tokens=20, output_tokens=10),
            response_id=f"scripted-{count}",
        )

    def final(value):
        return ResponseOutputMessage(
            id="final",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(type="output_text", text=value.model_dump_json(), annotations=[])
            ],
        )

    class SourceModel(GmailModel):
        async def get_response(self, *args, **kwargs):
            self.calls += 1
            gmail_inputs.append(
                json.dumps(
                    [
                        json.loads(row["output"])
                        for row in kwargs["input"]
                        if row.get("type") == "function_call_output"
                    ],
                    ensure_ascii=False,
                )
            )
            if self.calls == 1:
                name, arguments = (
                    "query_gmail_message_summaries",
                    {"query": "conditions", "label": "", "max_results": 1},
                )
            elif self.calls == 2:
                name, arguments = (
                    "read_gmail_context",
                    {"resource_type": "message", "resource_id": "msg-selected"},
                )
            else:
                previous = json.loads(
                    next(
                        row["output"]
                        for row in reversed(kwargs["input"])
                        if row.get("type") == "function_call_output"
                    )
                )
                arguments = previous["body_evidence"][0].get("next_request")
                if arguments is None:
                    assert all(marker in gmail_inputs[-1] for marker in (MARKERS[0], MARKERS[2]))
                    return response([final(selection())], self.calls)
                name = "read_gmail_context"
            return response(
                [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name=name,
                        call_id=f"gmail-{self.calls}",
                        arguments=json.dumps(arguments),
                    )
                ],
                self.calls,
            )

    source_model = SourceModel()
    gmail_result = run_gmail_triage_sdk(
        "Review all conditions in the selected message.",
        live=True,
        run_config=build_local_run_config(Provider(source_model)),
        provider_selection_required=True,
        selected_context_callback=contexts.append,
    )
    assert len(contexts) == 1 and contexts[0].source_windows_read > 16
    assert MARKERS[1] in gmail_inputs[-1]
    db = f"sqlite:///{tmp_path / 'sdk-parent.db'}"
    store = SQLiteStore(db)
    raw_request = (
        "Review the newsletter, then research the conditions from its retained source evidence."
    )
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Conditions",
        request_text=raw_request,
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Conditions", object_type="topic"),
    )
    store.save_work_item(item)

    def gmail_boundary(_input, **kwargs):
        kwargs["selected_context_callback"](contexts[0])
        return gmail_result

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", gmail_boundary)
    result = workflow._try_live_gmail_agent_owned_triage(
        item,
        request=WorkflowRunRequest(request_text=raw_request, live_sdk=True),
        store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(raw_request),
    )
    assert result.advanced
    store.save_work_item(result.work_item)
    restored = SQLiteStore(db).get_work_item(item.id)
    assert "".join(p.evidence_excerpt for p in restored.sources[0].evidence_pages) == body
    research_inputs = []
    recovered_pages = []

    class ResearchModel(GmailModel):
        def __init__(self, typed_input):
            super().__init__()
            self.packet = json.loads(typed_input.source_context)

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            serialized = json.dumps(kwargs["input"], ensure_ascii=False)
            research_inputs.append(serialized)
            assert [tool.name for tool in kwargs["tools"]] == ["read_work_item_source_evidence"]
            source = self.packet["context_pack"]["source_refs"][0]
            if self.calls == 1:
                assert MARKERS[1] not in serialized
                assert not source["evidence_pages"]
                arguments = dict(
                    source_id=source["source_id"],
                    expected_snapshot_sha256=source["evidence_access"]["snapshot_sha256"],
                    page_index=0,
                    max_pages=3,
                )
            else:
                previous = json.loads(
                    next(
                        row["output"]
                        for row in reversed(kwargs["input"])
                        if row.get("type") == "function_call_output"
                    )
                )
                assert previous["status"] == "read"
                recovered_pages.extend(previous["pages"])
                arguments = previous["next_request"]
                if arguments is None:
                    assert "".join(page["evidence_excerpt"] for page in recovered_pages) == body
                    assert all(
                        marker
                        in json.dumps(
                            [
                                json.loads(row["output"])
                                for row in kwargs["input"]
                                if row.get("type") == "function_call_output"
                            ],
                            ensure_ascii=False,
                        )
                        for marker in MARKERS
                    )
                    citations = self.packet["source_catalog"]
                    identities = [source["source_id"] for source in citations]
                    brief = ResearchBrief(
                        target_name="Conditions",
                        target_type="topic",
                        summary=" ".join(MARKERS),
                        facts=[
                            {"text": marker, "source_ids": [identities[0]]} for marker in MARKERS
                        ],
                        sources=citations,
                        source_ids_used=identities,
                        decision={
                            "decision_owner": "specialist_agent",
                            "decision_stage": "research_source_selection",
                            "selected_candidate_ids": identities,
                            "needs_more_context": False,
                            "reasoning": "All retained source pages were inspected.",
                            "candidate_assessments": [
                                {
                                    "candidate_id": identity,
                                    "disposition": "selected",
                                    "rationale": "Source evidence read.",
                                }
                                for identity in identities
                            ],
                        },
                    )
                    return response([final(brief)], self.calls)
            return response(
                [
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="read_work_item_source_evidence",
                        call_id=f"research-{self.calls}",
                        arguments=json.dumps(arguments),
                    )
                ],
                self.calls,
            )

    def research_boundary(typed_input, **kwargs):
        return run_business_research_analyst_research_brief_sdk(
            typed_input,
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(ResearchModel(typed_input))),
            },
        )

    monkeypatch.setattr(
        workflow, "run_business_research_analyst_research_brief_sdk", research_boundary
    )
    request = WorkflowRunRequest(
        request_text="Research the conditions using only retained source evidence.",
        work_item_id=restored.id,
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        orchestrator_preflight={"selected_agent": "business_research_analyst"},
        live_sdk=True,
        live_search=False,
        save=True,
        database_url=db,
        sdk_session_enabled=False,
    )
    if graph:
        from langgraph.checkpoint.memory import MemorySaver

        from keystone_agents.langgraph_workflow import run_work_item_langgraph

        monkeypatch.setattr(
            workflow, "_maybe_synthesize_user_facing_response", lambda result, **_kwargs: result
        )
        outcome = run_work_item_langgraph(
            request, checkpointer=MemorySaver(), require_langgraph=True
        )
        assert "run_business_research" in outcome.node_path
        research = outcome.result
    else:
        research = workflow._advance_research(restored, request=request, store=SQLiteStore(db))
    assert research.advanced, research.blockers
    assert len(research_inputs) == (len(recovered_pages) + 2) // 3 + 1
    assert all(marker in research.human_summary for marker in MARKERS)
    (tmp_path / "model-visible-proof.json").write_text(
        json.dumps(
            {
                "scope": (
                    "Synthetic provider and prescribed local SDK decisions; "
                    "no network or paid model"
                ),
                "graph": graph,
                "gmail_read_windows": contexts[0].source_windows_read,
                "retained_windows": len(restored.sources[0].evidence_pages),
                "gmail_model_inputs": gmail_inputs,
                "research_model_inputs": research_inputs,
                "recovered_pages": recovered_pages,
                "final_summary": research.human_summary,
                "source_id": restored.sources[0].source_id,
                "source_url": restored.sources[0].url,
                "evidence_access": restored.sources[0].evidence_access.model_dump(mode="json"),
                "paid_requests": 0,
                "provider_writes": 0,
                "fresh_semantic_interpretation_proven": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def test_retained_reader_bounds_unicode_metadata_and_never_skips_fragments():
    from keystone_agents.schemas.source_evidence import SourceEvidencePage
    from keystone_agents.schemas.work_item import WorkItemSourceRef
    from keystone_agents.tools.work_item_source_tool import MAX_SOURCE_EVIDENCE_RESPONSE_BYTES

    pages = [
        SourceEvidencePage(
            evidence_excerpt="🧠" * 3000,
            provenance_json=json.dumps(
                {"quoted_attribution": "研究条件" * 1200}, ensure_ascii=False
            ),
        ),
        SourceEvidencePage(evidence_excerpt="FINAL PAGE", provenance_json="{}"),
    ]
    source = WorkItemSourceRef(
        source_id="synthetic-source", url="https://example.test/source", evidence_pages=pages
    )
    tool = build_work_item_source_evidence_tool([source])
    request = {
        "source_id": source.source_id,
        "expected_snapshot_sha256": source.evidence_access.snapshot_sha256,
    }
    fragments, recovered, seen = {}, [], set()
    while request is not None:
        cursor = (request.get("page_index", 0), request.get("page_offset", 0))
        assert cursor not in seen
        seen.add(cursor)
        raw = tool(**request)
        assert len(raw.encode("utf-8")) <= MAX_SOURCE_EVIDENCE_RESPONSE_BYTES
        result = json.loads(raw)
        if "pages" in result:
            recovered.extend(result["pages"])
        else:
            partial = fragments.setdefault(result["page_index"], "")
            assert len(partial) == result["page_offset"]
            partial += result["page_fragment"]
            fragments[result["page_index"]] = partial
            if result["page_end"] == result["page_json_char_count"]:
                recovered.append(json.loads(partial))
        request = result["next_request"]
    assert recovered == [page.model_dump(mode="json") for page in pages]
    assert len(seen) > len(pages)


def test_source_snapshot_digest_rejects_tampered_persisted_page():
    from pydantic import ValidationError

    from keystone_agents.schemas.source_evidence import SourceEvidencePage
    from keystone_agents.schemas.work_item import WorkItemSourceRef

    source = WorkItemSourceRef(
        source_id="synthetic-source",
        evidence_pages=[SourceEvidencePage(evidence_excerpt="verified")],
    )
    payload = source.model_dump(mode="json")
    payload["evidence_pages"][0]["evidence_excerpt"] = "modified after selection"
    with pytest.raises(ValidationError, match="snapshot"):
        WorkItemSourceRef.model_validate(payload)


def test_new_selected_snapshot_replaces_prior_active_evidence(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from keystone_agents.business_research_analyst.supplied_context import supplied_research_sources
    from keystone_agents.models import TypedAgentRunResult

    _body, raw = install_provider(monkeypatch, repeats=400)
    previous = selected_provider_context(selection(), read_entries())
    database_url = f"sqlite:///{tmp_path / 'fresh-snapshot.db'}"
    store = SQLiteStore(database_url)
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD,
        title="Conditions",
        request_text="Review conditions",
        current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Conditions", object_type="topic"),
    )
    store.save_work_item(item)
    contexts = [previous]

    def sdk_boundary(_input, **kwargs):
        kwargs["selected_context_callback"](contexts[0])
        return TypedAgentRunResult(
            agent_name="gmail_triage",
            output=selection(),
            raw_result=SimpleNamespace(new_items=[]),
            live=True,
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", sdk_boundary)
    request = WorkflowRunRequest(request_text=item.request_text, live_sdk=True)
    first = workflow._try_live_gmail_agent_owned_triage(
        item,
        request=request,
        store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(item.request_text),
    )
    raw["historyId"] = "11"
    raw["payload"]["body"]["data"] = base64.urlsafe_b64encode(
        b"Updated condition: permission revoked."
    ).decode()
    contexts[0] = selected_provider_context(selection(), read_entries())
    second = workflow._try_live_gmail_agent_owned_triage(
        first.work_item,
        request=request,
        store=store,
        gmail_plan=workflow.resolve_gmail_execution_plan(item.request_text),
    )
    assert second.advanced
    active = supplied_research_sources(second.work_item)
    assert len(active) == 1
    assert active[0].source_id != first.work_item.sources[0].source_id
    assert active[0].evidence_pages[0].evidence_excerpt == "Updated condition: permission revoked."
    assert [artifact.selected for artifact in second.work_item.artifact_refs] == [False, True]


def test_missing_middle_window_remains_incomplete_with_exact_recovery_metadata(monkeypatch):
    install_provider(monkeypatch, repeats=400)
    entries = read_entries()
    assert len(entries) >= 3
    selected = selected_provider_context(selection(), [entries[0], *entries[2:]])
    assert selected is not None
    assert selected.source_evidence_complete is False
    assert selected.body_content_complete is False
    ref = gmail_context_source_refs(selected)[0]
    assert ref.evidence_access.page_count == len(entries) - 1
    first = json.loads(ref.evidence_pages[0].provenance_json)
    assert first["next_request"]["body_start_char"] == 3000
    assert (
        first["next_request"]["expected_source_snapshot_sha256"] == selected.source_snapshot_sha256
    )


@pytest.mark.parametrize(
    ("rejection", "expected_status"),
    [
        ("unknown_source", "not_found"),
        ("oversized_identity", "not_found"),
        ("snapshot_mismatch", "source_changed"),
        ("negative_page", "out_of_range"),
        ("past_last_page", "out_of_range"),
        ("invalid_page_limit", "out_of_range"),
        ("negative_offset", "out_of_range"),
        ("past_page_end", "out_of_range"),
        ("oversized_metadata", "source_metadata_too_large"),
    ],
)
def test_retained_reader_rejections_are_failed_execution_evidence(rejection, expected_status):
    from keystone_agents.runtime.tool_execution import tool_result_succeeded
    from keystone_agents.schemas.source_evidence import SourceEvidencePage
    from keystone_agents.schemas.work_item import WorkItemSourceRef
    from keystone_agents.tools.work_item_source_tool import MAX_SOURCE_EVIDENCE_RESPONSE_BYTES

    source = WorkItemSourceRef(
        source_id="synthetic-source",
        url="https://example.test/"
        + ("x" * 20_000 if rejection == "oversized_metadata" else "source"),
        evidence_pages=[SourceEvidencePage(evidence_excerpt="Verified source text")],
    )
    request = {
        "source_id": source.source_id,
        "expected_snapshot_sha256": source.evidence_access.snapshot_sha256,
    }
    request.update(
        {
            "unknown_source": {"source_id": "unselected"},
            "oversized_identity": {"source_id": "x" * 201},
            "snapshot_mismatch": {"expected_snapshot_sha256": "0" * 64},
            "negative_page": {"page_index": -1},
            "past_last_page": {"page_index": 1},
            "invalid_page_limit": {"max_pages": 4},
            "negative_offset": {"page_offset": -1},
            "past_page_end": {"page_offset": 100_000},
        }.get(rejection, {})
    )
    raw = build_work_item_source_evidence_tool([source])(**request)
    output = json.loads(raw)
    assert output["status"] == expected_status
    assert output["success"] is False
    assert "pages" not in output and "page_fragment" not in output
    assert not tool_result_succeeded(raw)
    assert not tool_result_succeeded(output)
    assert len(raw.encode("utf-8")) <= MAX_SOURCE_EVIDENCE_RESPONSE_BYTES


@pytest.mark.parametrize("fragmented", [False, True])
def test_retained_reader_data_is_successful_execution_evidence(fragmented):
    from keystone_agents.runtime.tool_execution import tool_result_succeeded
    from keystone_agents.schemas.source_evidence import SourceEvidencePage
    from keystone_agents.schemas.work_item import WorkItemSourceRef
    from keystone_agents.tools.work_item_source_tool import MAX_SOURCE_EVIDENCE_RESPONSE_BYTES

    source = WorkItemSourceRef(
        source_id="synthetic-source",
        url="https://example.test/source",
        evidence_pages=[
            SourceEvidencePage(
                evidence_excerpt="Verified source text",
                provenance_json=json.dumps({"description": "🧠" * 8000}, ensure_ascii=False)
                if fragmented
                else "{}",
            )
        ],
    )
    raw = build_work_item_source_evidence_tool([source])(
        source_id=source.source_id,
        expected_snapshot_sha256=source.evidence_access.snapshot_sha256,
    )
    output = json.loads(raw)
    assert output["status"] == "read" and output["success"] is True
    assert bool(output.get("page_fragment")) if fragmented else bool(output.get("pages"))
    assert tool_result_succeeded(raw)
    assert tool_result_succeeded(output)
    assert len(raw.encode("utf-8")) <= MAX_SOURCE_EVIDENCE_RESPONSE_BYTES
