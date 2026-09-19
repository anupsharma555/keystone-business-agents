"""Supplied and selected-URL evidence must reach the registered Research SDK."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import workflow_runner
from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.business_research_analyst.supplied_context import (
    supplied_research_input,
    supplied_research_sources,
)
from keystone_agents.langgraph_workflow import run_work_item_langgraph
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage import SQLiteStore

SOURCE_ID = "supplied-saffron-73"
SOURCE_URL = "https://example.test/saffron-study"
REQUEST = "Use only the supplied context to assess the evidence and state what is still unknown."
SUMMARY = (
    "The retrospective signal warrants prospective evaluation; deployment readiness is unproven."
)


def _item():
    return WorkItem(
        id="wi_supplied_research",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Evaluate Saffron 73",
        request_text=REQUEST,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(
            name="Saffron 73",
            object_type="topic",
            metadata={
                "external_context": {"schema": "keystone.work_item.source_bundle.v1"},
            },
        ),
        sources=[
            WorkItemSourceRef(
                source_id=SOURCE_ID,
                url=SOURCE_URL,
                title="Saffron evidence",
                source_type="public_article",
                provider="operator_supplied",
                provider_candidate_id="retained-provider-identity",
                extraction_status="supplied_material",
                evidence_excerpt=(
                    "The violet cohort contained 37 observations; no prospective trial exists."
                ),
                key_facts=["The meridian control was absent."],
            )
        ],
    )


def _output():
    return ResearchBrief(
        target_name="Saffron 73",
        target_type="topic",
        summary=SUMMARY,
        facts=[{"text": "The cohort included 37 observations.", "source_ids": [SOURCE_ID]}],
        limitations=["Prospective utility was not measured."],
        sources=[
            {
                "source_id": SOURCE_ID,
                "url": SOURCE_URL,
                "title": "Saffron evidence",
                "source_type": "public_article",
            }
        ],
        source_ids_used=[SOURCE_ID],
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "research_source_selection",
            "selected_candidate_ids": [SOURCE_ID],
            "needs_more_context": False,
            "reasoning": "Select the supplied evidence without inferring a prospective outcome.",
            "candidate_assessments": [
                {
                    "candidate_id": SOURCE_ID,
                    "disposition": "selected",
                    "rationale": "Direct supplied evidence.",
                }
            ],
        },
    ).model_dump(mode="json")


class ScriptedResearch(Model):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        **kwargs,
    ):
        rendered = json.dumps(input)
        for marker in (
            REQUEST,
            SOURCE_ID,
            SOURCE_URL,
            "violet cohort",
            "37 observations",
            "meridian control was absent",
            "Saffron 73",
            "research_goal",
        ):
            assert marker in rendered
        assert not tools and not handoffs
        self.calls.append(rendered)
        payload = self.outputs.pop(0)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"research-{len(self.calls)}",
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
            ],
            usage=Usage(requests=1, input_tokens=30, output_tokens=20),
            response_id=f"research-response-{len(self.calls)}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def _install(monkeypatch, model):
    actual = run_business_research_analyst_research_brief_sdk

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["provider_retrieval_required"] is False
        assert tuple(source.source_id for source in kwargs["supplied_sources"]) == (SOURCE_ID,)
        return actual(
            typed_input, **{**kwargs, "run_config": build_local_run_config(Provider(model))}
        )

    monkeypatch.setattr(workflow_runner, "run_business_research_analyst_research_brief_sdk", invoke)

    def unexpected(*args, **kwargs):
        pytest.fail("Supplied evidence cannot call retrieval or template company synthesis.")

    for name in (
        "retrieve_company_profile_live",
        "research_company_fixture",
        "synthesize_company_profile_from_source_bundle",
        "build_contact_enrichment_artifact",
    ):
        monkeypatch.setattr(workflow_runner, name, unexpected)


@pytest.mark.parametrize("graph", [False, True])
def test_supplied_evidence_runs_native_sdk_and_retains_model_output_and_accounting(
    tmp_path,
    monkeypatch,
    graph,
):
    if graph:
        pytest.importorskip("langgraph")
    model = ScriptedResearch([_output()])
    _install(monkeypatch, model)
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    store = SQLiteStore(database_url)
    work_item = _item()
    store.save_work_item(work_item)
    request = WorkflowRunRequest(
        request_text=REQUEST,
        work_item_id=work_item.id,
        database_url=database_url,
        live_sdk=True,
        live_search=False,
        sdk_session_enabled=False,
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        model_request_limit=1,
    )
    result = (
        run_work_item_langgraph(request, require_langgraph=True).result
        if graph
        else workflow_runner.advance_work_item(request)
    )
    assert result.advanced, result.human_summary
    assert len(model.calls) == 1
    assert SUMMARY in result.human_summary
    assert SOURCE_URL in result.human_summary
    assert result.work_item.target.name == "Saffron 73"
    artifact = result.artifact_refs[0]
    assert artifact.artifact_type == "research_brief"
    assert artifact.selected is True
    assert artifact.metadata["research_brief"] == _output()
    assert (
        artifact.metadata["source_refs"][0]["provider_candidate_id"] == "retained-provider-identity"
    )
    assert all(
        source.provider not in {"fixture", "promptfoo"} for source in result.work_item.sources
    )
    row = store.get_agent_run(artifact.metadata["agent_run_id"])
    assert row["status"] == "success" and not row["dry_run"]
    assert row["output"]["work_item_id"] == work_item.id
    assert row["output"]["usage"]["input_tokens"] == 30
    assert row["output"]["summary"] == SUMMARY
    events = [
        event
        for event in store.list_work_item_events(work_item.id)
        if event.event_type == "workflow_sdk_usage"
    ]
    assert sum(event.metadata["usage"]["requests"] for event in events) == 1


@pytest.mark.parametrize("fault", ["changed_url", "missing_citation", "unknown_identity"])
def test_invalid_research_never_becomes_a_fixture_or_accepted_artifact(
    tmp_path, monkeypatch, fault
):
    payload = _output()
    if fault == "changed_url":
        payload["sources"][0]["url"] = "https://example.test/unsupplied"
    elif fault == "missing_citation":
        payload["facts"][0]["source_ids"] = ["absent-source"]
    else:
        payload["decision"]["selected_candidate_id"] = "absent-source"
        payload["decision"]["selected_candidate_ids"] = ["absent-source"]
        payload["decision"]["candidate_assessments"][0]["candidate_id"] = "absent-source"
    model = ScriptedResearch([deepcopy(payload), deepcopy(payload)])
    _install(monkeypatch, model)
    store = SQLiteStore(f"sqlite:///{tmp_path / 'failed.db'}")
    store.save_work_item(_item())
    result = workflow_runner._supplied_business_research_sdk_result(
        _item(),
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        store=store,
    )
    assert not result.advanced
    assert not result.artifact_refs
    assert not result.work_item.artifact_refs
    assert len(model.calls) == 2  # Shared bounded decision repair, with no new retrieval.
    events = [
        event
        for event in store.list_work_item_events(_item().id)
        if event.event_type == "workflow_sdk_usage"
    ]
    assert events[0].metadata["usage"]["requests"] == 2
    assert events[0].metadata["usage"]["input_tokens"] == 60


@pytest.mark.parametrize("fault", ["empty", "fixture", "conflict", "oversize", "readiness"])
def test_bad_supplied_evidence_blocks_before_model(monkeypatch, fault):
    item = _item()
    if fault == "empty":
        item.sources[0].evidence_excerpt = ""
        item.sources[0].key_facts = []
    elif fault == "fixture":
        item.sources[0].provider = "promptfoo"
    elif fault == "conflict":
        item.sources.append(
            item.sources[0].model_copy(update={"url": "https://example.test/other"})
        )
    elif fault == "oversize":
        item.sources[0].evidence_excerpt = "x" * 24_001
    else:
        item.target.name = ""
    model = ScriptedResearch([])
    _install(monkeypatch, model)
    result = workflow_runner._supplied_business_research_sdk_result(
        item,
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        store=None,
    )
    assert not result.advanced and not model.calls and not result.artifact_refs


def test_inline_operator_note_has_honest_internal_source_and_no_write_grants():
    item = _item().model_copy(update={"sources": []})
    sources = supplied_research_sources(item, inline_context="A synthetic operator-supplied note.")
    assert sources[0].source_id.startswith("operator-note:")
    assert sources[0].url.startswith("provided://operator-note/")
    typed, _ = supplied_research_input(
        item, raw_request=REQUEST, sources=sources, orchestration_context={"raw_request": REQUEST}
    )
    packet = json.loads(typed.source_context)
    assert packet["retrieval_enabled"] is False
    assert packet["context_pack"]["approval_gates"] == []


@pytest.mark.parametrize("extraction_status", ["success", "empty", "failed"])
def test_exact_url_extraction_precedes_sdk_and_empty_or_failed_reads_never_call_it(
    tmp_path,
    monkeypatch,
    extraction_status,
):
    from keystone_agents.schemas.company_profile import SourceRecord
    from keystone_agents.source_enrichment import SourceBundle
    from keystone_agents.tools.website_extraction_tool import SelectedUrlSourceBundleResult

    model = ScriptedResearch([_output()])
    _install(monkeypatch, model)
    item = _item().model_copy(update={"sources": []})
    item.target.url = SOURCE_URL
    request_text = f"{REQUEST} Read only this selected public URL: {SOURCE_URL}"
    captured = []

    def extract(**kwargs):
        captured.append(kwargs)
        assert kwargs["selected_urls"] == [SOURCE_URL]
        assert kwargs["live_extraction"] is True
        if extraction_status == "failed":
            raise workflow_runner.WebsiteExtractionError("Synthetic extraction failure.")
        source = SourceRecord(
            source_id=SOURCE_ID,
            title="Saffron evidence",
            url=SOURCE_URL,
            source_type="website",
            supported_claims=["The meridian control was absent."],
            evidence_excerpt=_item().sources[0].evidence_excerpt,
            confidence=0.8,
        )
        return SelectedUrlSourceBundleResult(
            mode="live",
            company_name="Saffron 73",
            selected_url_count=1,
            extracted_source_count=1 if extraction_status == "success" else 0,
            source_bundle=SourceBundle(
                company_name="Saffron 73",
                company_url=SOURCE_URL,
                sources=[source] if extraction_status == "success" else [],
                claim_candidates=source.supported_claims if extraction_status == "success" else [],
            ),
        )

    monkeypatch.setattr(workflow_runner, "build_selected_url_source_bundle", extract)
    store = SQLiteStore(f"sqlite:///{tmp_path / 'selected.db'}")
    store.save_work_item(item)
    result = workflow_runner._advance_selected_url_business_research(
        item,
        request=WorkflowRunRequest(request_text=request_text, live_sdk=True),
        target=item.target.name,
        manual_plan={},
        store=store,
    )
    assert len(captured) == 1
    if extraction_status == "success":
        assert result.advanced and len(model.calls) == 1
        assert SUMMARY in result.human_summary and SOURCE_URL in result.human_summary
        artifact = result.artifact_refs[0]
        assert artifact.artifact_type == "research_brief"
        assert artifact.metadata["source_refs"][0]["source_id"] == SOURCE_ID
        assert artifact.metadata["retrieval_diagnostics"]["broad_search_performed"] is False
    else:
        assert not result.advanced and not model.calls and not result.artifact_refs


def test_research_agent_can_request_context_without_a_success_artifact(tmp_path, monkeypatch):
    output = _output()
    output.update(
        summary="Please supply prospective evidence before a readiness recommendation.",
        sources=[],
        facts=[],
        source_ids_used=[],
    )
    output["decision"].update(
        needs_more_context=True,
        selected_candidate_id="",
        selected_candidate_ids=[],
        candidate_assessments=[
            {
                "candidate_id": SOURCE_ID,
                "disposition": "excluded",
                "rationale": "Retrospective evidence cannot answer readiness.",
            }
        ],
    )
    model = ScriptedResearch([output])
    _install(monkeypatch, model)
    store = SQLiteStore(f"sqlite:///{tmp_path / 'needs-context.db'}")
    store.save_work_item(_item())
    result = workflow_runner._supplied_business_research_sdk_result(
        _item(),
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        store=store,
    )
    assert len(model.calls) == 1 and not result.advanced and not result.artifact_refs
    assert result.human_summary == output["summary"]


def test_live_supplied_bundle_preserves_accepted_target_over_heuristic_topic(
    tmp_path,
    monkeypatch,
    supplied_research_fake_sdk,
):
    item = _item()
    item.target.metadata["manual_request_plan"] = {
        "source": "heuristic",
        "primary_target": "Unrelated inferred topic",
    }
    store = SQLiteStore(f"sqlite:///{tmp_path / 'target.db'}")
    store.save_work_item(item)
    result = workflow_runner._advance_research(
        item,
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        store=store,
    )
    assert result.advanced
    assert result.work_item.target.name == "Saffron 73"
    assert supplied_research_fake_sdk[0]["typed_input"].target_name == "Saffron 73"


def test_accepted_research_brief_reaches_actual_thread_local_outreach_model(
    tmp_path,
    monkeypatch,
):
    from keystone_agents.run import run_retrieved_sdk_synthesis
    from keystone_agents.schemas.outreach import OutreachLLMDraftPayload

    research_model = ScriptedResearch([_output()])
    _install(monkeypatch, research_model)
    store = SQLiteStore(f"sqlite:///{tmp_path / 'handoff.db'}")
    item = _item()
    store.save_work_item(item)
    research = workflow_runner._supplied_business_research_sdk_result(
        item,
        request=WorkflowRunRequest(request_text=REQUEST, live_sdk=True),
        store=store,
    )
    assert research.advanced
    selected_ids = [SOURCE_ID, "keystone_profile"]
    outreach_payload = OutreachLLMDraftPayload(
        company_name="Saffron 73",
        email_subject="Prospective evaluation discussion",
        email_body=(
            "Hello,\n\nCould we discuss a bounded prospective evaluation of Saffron 73? "
            "The retrospective cohort included 37 observations, and deployment readiness "
            "remains unproven. We could first clarify the intended decision and evaluation "
            "design.\n\nSincerely,\nKeystone"
        ),
        personalization_rationale="Uses the accepted Research findings and limitations.",
        source_ids_used=selected_ids,
        decision={
            "decision_owner": "specialist_agent",
            "decision_stage": "outreach_evidence_selection",
            "selected_candidate_ids": selected_ids,
            "needs_more_context": False,
            "reasoning": "Select accepted research evidence and approved Keystone positioning.",
            "candidate_assessments": [
                {
                    "candidate_id": identity,
                    "disposition": "selected",
                    "rationale": "Supports the requested draft.",
                }
                for identity in selected_ids
            ]
            + [
                {
                    "candidate_id": "user_provided:thread_local_request",
                    "disposition": "excluded",
                    "rationale": "Instruction context, not a factual claim.",
                }
            ],
        },
    )
    observed = []

    class OutreachModel(Model):
        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            serialized = json.dumps(input)
            for marker in (
                SUMMARY,
                SOURCE_ID,
                SOURCE_URL,
                "37 observations",
                "Prospective utility was not measured",
                "research_brief",
                "selected_artifacts",
                "outreach",
            ):
                assert marker in serialized
            assert not tools and not handoffs
            observed.append(serialized)
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="outreach-handoff-response",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text",
                                text=outreach_payload.model_dump_json(),
                                annotations=[],
                            )
                        ],
                    )
                ],
                usage=Usage(requests=1, input_tokens=45, output_tokens=25),
                response_id="outreach-handoff",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    def invoke(**kwargs):
        return run_retrieved_sdk_synthesis(
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(OutreachModel())),
            }
        )

    monkeypatch.setattr(workflow_runner, "run_retrieved_sdk_synthesis", invoke)
    next_item = research.work_item.model_copy(
        update={"current_route": WorkItemRoute.OUTREACH_COMPOSER}
    )
    draft, _note, usage, _recommendation = (
        workflow_runner._compose_thread_local_outreach_draft_for_work_item(
            request=WorkflowRunRequest(
                request_text=(
                    "Prepare draft-only Slack-thread sample outreach using the accepted research."
                ),
                live_sdk=True,
            ),
            work_item=next_item,
            target_name=next_item.target.name,
        )
    )
    assert len(research_model.calls) == len(observed) == 1, (_note, usage)
    assert usage["usage"]["input_tokens"] == 45
    assert draft.email_body == outreach_payload.email_body
    assert SOURCE_ID in draft.source_ids_used
    assert not draft.send_enabled and not draft.sent and not draft.external_use_allowed
    assert not store.list_approval_items(object_type="outreach_draft")


@pytest.mark.parametrize("graph", [False, True])
def test_public_workflow_blocks_invalid_research_without_template_fallback(
    tmp_path,
    monkeypatch,
    graph,
):
    if graph:
        pytest.importorskip("langgraph")
    invalid = _output()
    invalid["sources"][0]["url"] = "https://example.test/unsupplied"
    model = ScriptedResearch([deepcopy(invalid), deepcopy(invalid)])
    _install(monkeypatch, model)
    database_url = f"sqlite:///{tmp_path / 'public-failed.db'}"
    store = SQLiteStore(database_url)
    item = _item()
    store.save_work_item(item)
    request = WorkflowRunRequest(
        work_item_id=item.id,
        request_text=REQUEST,
        live_sdk=True,
        database_url=database_url,
        sdk_session_enabled=False,
        model_request_limit=2,
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    result = (
        run_work_item_langgraph(request, require_langgraph=True).result
        if graph
        else workflow_runner.advance_work_item(request)
    )
    assert not result.advanced and not result.artifact_refs
    assert not store.get_work_item(item.id).artifact_refs
    assert len(model.calls) == 2
    failed_rows = [
        row
        for row in store.fetch_all("agent_runs")
        if row["agent_name"] == "business_research_analyst"
    ]
    assert len(failed_rows) == 1 and failed_rows[0]["status"] == "blocked"
    payload = json.loads(failed_rows[0]["output_json"])
    assert payload["usage"]["requests"] == 2
    assert payload["usage"]["input_tokens"] == 60


@pytest.mark.parametrize("draft_requested", [False, True])
def test_supplied_research_retains_only_explicit_thread_local_drafting_eligibility(
    tmp_path,
    supplied_research_fake_sdk,
    draft_requested,
):
    item = _item()
    store = SQLiteStore(f"sqlite:///{tmp_path / 'scope.db'}")
    store.save_work_item(item)
    raw_request = REQUEST + (
        " Then prepare a draft-only Slack-thread sample outreach for review. Do not send or post."
        if draft_requested
        else ""
    )
    result = workflow_runner._supplied_business_research_sdk_result(
        item,
        request=WorkflowRunRequest(request_text=raw_request, live_sdk=True),
        store=store,
    )
    assert result.advanced and len(supplied_research_fake_sdk) == 1
    artifact = result.artifact_refs[0]
    assert artifact.metadata["operator_approved_thread_local_drafting"] is draft_requested
    assert artifact.approval_state == (
        "approved_for_drafting" if draft_requested else "approved_for_research"
    )
    assert result.next_action.agent == (
        WorkItemRoute.OUTREACH_COMPOSER
        if draft_requested
        else WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    )
    assert not artifact.metadata["research_brief"]["send_enabled"]
    assert not store.list_approval_items(object_type="outreach_draft")
