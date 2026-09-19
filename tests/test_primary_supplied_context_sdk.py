"""Owning SDK execution for supplied Gmail and source-followup semantic work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import workflow_runner as workflow
from keystone_agents.agents.gmail_triage import triage_email_fixture
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore


class SuppliedModel(Model):
    def __init__(self, payload):
        self.payload = payload
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
        assert tools == [] and handoffs == []
        self.calls.append(input if isinstance(input, str) else json.dumps(input))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="synthetic-supplied-output",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
                )
            ],
            usage=Usage(requests=1, input_tokens=100, output_tokens=100, total_tokens=200),
            response_id="synthetic-supplied-response",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def prepare_case(tmp_path, owner):
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    store = SQLiteStore(database_url)
    if owner == "gmail":
        request_text = (
            "Review the supplied Gmail message read-only; do not create a provider draft or send."
        )
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures/graph_research_to_draft_source_bundle.json"
            ).read_text()
        )
        item = workflow._apply_external_context(
            WorkItem(
                kind=WorkItemKind.GMAIL_THREAD,
                title="Synthetic supplied message",
                request_text=request_text,
            ),
            fixture,
            context_file_path="",
        )
        message = workflow._gmail_fixture_from_work_item_context(item)
        output = triage_email_fixture(message).model_copy(
            update={
                "summary": "The owning SDK classified the supplied message.",
                "reasoning": "The supplied evidence supports this classification.",
            }
        )
        route = WorkItemRoute.GMAIL_TRIAGE
    else:
        request_text = "Explain source 2 and its limitations."
        item = WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Synthetic source followup",
            request_text=request_text,
            sources=[
                WorkItemSourceRef(
                    source_id=f"synthetic-source-{index}",
                    title=f"Synthetic source {index}",
                    url=f"https://example.test/source-{index}",
                    supported_claim=f"Source {index} supplied evidence.",
                    evidence_excerpt=f"Source {index} supports a bounded synthetic observation.",
                )
                for index in (1, 2)
            ],
            approval_gates=[
                WorkItemApprovalGate(scope="external_use", state="pending", required=True)
            ],
        )
        output = ChiefOfStaffResult(
            mode="llm",
            summary="The owning SDK explained the selected source.",
            synthesis="Source 2 supports only the supplied bounded observation.",
            recommended_route=ChiefOfStaffRouteRecommendation(workflow_type="slack-article-review"),
            sources=[{"title": "Synthetic source 2", "url": "https://example.test/source-2"}],
            decision=AgentDecisionRecord(
                decision_owner="chief_of_staff",
                decision_stage="chief_delegation_selection",
                selected_candidate_id="workflow:slack-article-review",
                candidate_assessments=[
                    {
                        "candidate_id": "workflow:slack-article-review",
                        "disposition": "selected",
                        "rationale": "Explain supplied source evidence.",
                    }
                ],
                reasoning="The request is a bounded source explanation.",
            ),
        )
        route = WorkItemRoute.CHIEF_OF_STAFF
    item = item.model_copy(update={"current_route": route})
    store.save_work_item(item)
    request = WorkflowRunRequest(
        request_text=request_text,
        work_item_id=item.id,
        requested_route=route,
        database_url=database_url,
        save=True,
        live_sdk=True,
        live_search=False,
        sdk_session_enabled=False,
    )
    return store, request, output


def execute(prepared, lane):
    if lane == "direct":
        return workflow.run_prepared_work_item_specialist(prepared)
    pytest.importorskip("langgraph.graph")
    from langgraph.graph import END, START, StateGraph

    from keystone_agents import langgraph_workflow as graph_runtime

    name = (
        "run_gmail_triage" if prepared.route == WorkItemRoute.GMAIL_TRIAGE else "run_chief_of_staff"
    )
    builder = StateGraph(graph_runtime.WorkItemGraphState)
    builder.add_node(name, getattr(graph_runtime, f"_{name}_node"))
    builder.add_edge(START, name)
    builder.add_edge(name, END)
    state = builder.compile().invoke(
        {
            "request": prepared.request.model_dump(mode="json"),
            "prepared_step": graph_runtime._prepared_step_payload(prepared),
            "node_path": [],
        }
    )
    assert state["node_path"] == [name]
    return WorkflowRunResult.model_validate(state["result"])


@pytest.mark.parametrize("owner", ["gmail", "chief"])
@pytest.mark.parametrize("lane", ["direct", "graph"])
@pytest.mark.parametrize("invalid", [False, True])
def test_supplied_semantic_work_runs_owning_sdk_and_never_accepts_fixture_on_failure(
    tmp_path, monkeypatch, owner, lane, invalid
):
    store, request, output = prepare_case(tmp_path, owner)
    model = SuppliedModel("{" if invalid else output.model_dump(mode="json"))
    sdk_name = "run_gmail_triage_sdk" if owner == "gmail" else "run_chief_of_staff_sdk"
    actual_sdk = getattr(workflow, sdk_name)
    typed_inputs = []

    def local_sdk(typed_input, **kwargs):
        assert kwargs["live"] is True
        assert kwargs["attach_tools"] is False
        if owner == "gmail":
            assert kwargs["provider_selection_required"] is False
            assert kwargs["provider_context_read_required"] is False
            assert typed_input.message_id == "message-northstar-001"
            assert typed_input.thread_id == "thread-northstar-001"
            assert request.request_text in typed_input.request
            assert "fixture:graph-source:inbound-email" in typed_input.request
        else:
            assert typed_input["request"] == request.request_text
            assert typed_input["selected_source_followup"]["source_id"] == "synthetic-source-2"
            assert [source["source_id"] for source in typed_input["work_item"]["sources"]] == [
                "synthetic-source-2"
            ]
            assert "Source 2 supports" in json.dumps(typed_input["selected_source_context"])
        typed_inputs.append(typed_input)
        return actual_sdk(
            typed_input, **{**kwargs, "run_config": build_local_run_config(Provider(model))}
        )

    monkeypatch.setattr(workflow, sdk_name, local_sdk)
    monkeypatch.setattr(
        workflow, "triage_email_fixture", lambda *_: pytest.fail("Live work used fixture triage")
    )
    monkeypatch.setattr(
        workflow,
        "_chief_source_link_followup_result",
        lambda *_args, **_kwargs: pytest.fail("Live work used deterministic source summary"),
    )
    monkeypatch.setattr(
        workflow,
        "read_linked_article_impl",
        lambda *_args, **_kwargs: pytest.fail("Unapproved source provider read"),
    )
    prepared = workflow.prepare_work_item_step(request)
    with activate_model_request_budget(1) as budget:
        result = execute(prepared, lane)
        assert len(typed_inputs) == len(model.calls) == budget.consumed == 1
    events = [
        event
        for event in store.list_work_item_events(result.work_item.id)
        if event.event_type == "workflow_sdk_usage"
    ]
    assert len(events) == 1 and events[0].metadata["usage"]["requests"] == 1
    assert result.work_item.id == request.work_item_id
    if invalid:
        assert result.advanced is False
        assert not result.artifact_refs
        assert any(
            "agent_decision_invalid" in b.code or "decision_unavailable" in b.code
            for b in result.blockers
        )
    else:
        assert result.advanced is True
        rows = [
            row
            for row in store.fetch_all("agent_runs")
            if row["agent_name"] == prepared.route.value
        ]
        assert len(rows) == 1 and rows[0]["dry_run"] == 0 and rows[0]["model"] != "fixture"
        assert "owning SDK" in rows[0]["output_json"]
        assert "source_link_summary" not in {
            artifact.artifact_type for artifact in result.artifact_refs
        }
    if owner == "chief":
        assert result.work_item.approval_gates[0].state == "pending"


@pytest.mark.parametrize("owner", ["gmail", "chief"])
def test_no_live_sdk_retains_fixture_execution(tmp_path, monkeypatch, owner):
    _store, request, _output = prepare_case(tmp_path, owner)
    request = request.model_copy(update={"live_sdk": False})
    for name in ("run_gmail_triage_sdk", "run_chief_of_staff_sdk"):
        monkeypatch.setattr(
            workflow, name, lambda *_args, **_kwargs: pytest.fail("Fixture requested SDK")
        )
    prepared = workflow.prepare_work_item_step(request)
    with activate_model_request_budget(0) as budget:
        result = execute(prepared, "direct")
        assert budget.consumed == 0
    assert result.advanced
    expected = "gmail_triage_report" if owner == "gmail" else "source_link_summary"
    assert expected in {artifact.artifact_type for artifact in result.artifact_refs}


def test_supplied_gmail_rejects_model_changed_message_identity(tmp_path, monkeypatch):
    store, request, output = prepare_case(tmp_path, "gmail")
    model = SuppliedModel(
        output.model_copy(update={"message_id": "different-message"}).model_dump(mode="json")
    )
    actual_sdk = workflow.run_gmail_triage_sdk

    def local_sdk(typed_input, **kwargs):
        return actual_sdk(
            typed_input, **{**kwargs, "run_config": build_local_run_config(Provider(model))}
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", local_sdk)
    result = execute(workflow.prepare_work_item_step(request), "direct")
    assert len(model.calls) == 1
    assert not result.advanced and not result.artifact_refs
    assert store.fetch_all("agent_runs") == []
    assert any(
        blocker.code == "gmail_triage_agent_decision_invalid" for blocker in result.blockers
    )


@pytest.mark.parametrize("sender", [
    "Sender <sender@example.test>",
    "Jordan Lee, Operations at Cedar Valley Rehab",
])
def test_inline_message_live_sdk_preserves_operator_wording_without_provider_tools(
    monkeypatch, sender,
):
    request_text = (
        "Triage this email read-only. Subject: Synthetic project. "
        f"From: {sender}. Body: Could we discuss a research collaboration?"
    )
    message = workflow._inline_gmail_fixture_from_request(request_text)
    output = triage_email_fixture(message).model_copy(
        update={"summary": "Model interpreted inline evidence."}
    )
    model = SuppliedModel(output.model_dump(mode="json"))
    actual_sdk = workflow.run_gmail_triage_sdk

    def local_sdk(typed_input, **kwargs):
        assert request_text in typed_input.request
        assert "research collaboration" in typed_input.body
        assert typed_input.sender_name == message.sender_name
        assert typed_input.sender_email == message.sender_email
        assert kwargs["attach_tools"] is False
        return actual_sdk(
            typed_input, **{**kwargs, "run_config": build_local_run_config(Provider(model))}
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", local_sdk)
    monkeypatch.setattr(
        workflow, "triage_email_fixture", lambda *_: pytest.fail("Live inline fallback")
    )
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD, title="Inline fixture", request_text=request_text
    )
    result = workflow._advance_gmail_triage(
        item,
        request=WorkflowRunRequest(
            request_text=request_text, live_sdk=True, save=False, sdk_session_enabled=False
        ),
        store=None,
    )
    assert len(model.calls) == 1 and result.advanced
    assert "Model interpreted inline evidence" in result.human_summary


@pytest.mark.parametrize("entrypoint", ["supplied", "provider_result"])
def test_gmail_preserves_canonical_research_company_over_publisher(
    tmp_path, monkeypatch, entrypoint,
):
    request_text = (
        "Review this newsletter and research Featured Company. "
        "Email: From: Newsletter Publisher <editor@publisher.example.test>. "
        "Subject: Company update. Body: Featured Company announced an evaluation pilot."
    )
    plan = {
        "source": "orchestrator_canonical", "target_agent": "gmail_triage",
        "intent": "gmail_triage", "workflow": ["gmail_triage", "business_research_analyst"],
        "primary_target": "Featured Company", "target_type": "company",
    }
    store = SQLiteStore(f"sqlite:///{tmp_path / 'target.db'}")
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD, title="Newsletter review",
        request_text=request_text, current_route=WorkItemRoute.GMAIL_TRIAGE,
        target=WorkItemTarget(name="Featured Company", object_type="company",
                              metadata={"manual_request_plan": plan}),
    )
    store.save_work_item(item)
    request = WorkflowRunRequest(
        request_text=request_text, live_sdk=True, sdk_session_enabled=False,
        manual_request_plan=plan,
    )
    message = workflow._inline_gmail_fixture_from_request(request_text)
    output = triage_email_fixture(message).model_copy(update={
        "summary": "Newsletter includes a company announcement.",
        "recommended_next_agent": "business_research_analyst",
    })
    model = SuppliedModel(output.model_dump(mode="json"))
    actual_sdk = workflow.run_gmail_triage_sdk

    def local_sdk(typed_input, **kwargs):
        assert request_text in typed_input.request
        assert "Featured Company" in typed_input.request
        if entrypoint == "provider_result":
            # This control isolates post-validation handoff; provider identity validation
            # has its own real SDK tests. No provider call is simulated as a live read.
            return TypedAgentRunResult(
                agent_name="gmail_triage", output=output, raw_result=None, live=True,
            )
        return actual_sdk(
            typed_input, **{**kwargs, "run_config": build_local_run_config(Provider(model))}
        )

    monkeypatch.setattr(workflow, "run_gmail_triage_sdk", local_sdk)
    if entrypoint == "supplied":
        result = workflow._advance_gmail_triage(item, request=request, store=store)
        assert len(model.calls) == 1
    else:
        result = workflow._try_live_gmail_agent_owned_triage(
            item, request=request, store=store,
            gmail_plan=workflow.resolve_gmail_execution_plan(
                request_text, manual_plan=plan,
            ),
        )
    assert result.advanced
    assert result.next_action.agent == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert result.work_item.target.name == "Featured Company"
    assert result.work_item.target.metadata["gmail_research_target"] == "Featured Company"
    assert result.work_item.target.metadata["manual_request_plan"]["primary_target"] == (
        "Featured Company"
    )
    assert result.artifact_refs[0].metadata["gmail_research_target"] == "Featured Company"


@pytest.mark.parametrize("source", ["heuristic", "test", "orchestrator_canonical"])
def test_gmail_target_preservation_requires_canonical_authority(source):
    plan = {
        "source": source, "target_agent": "gmail_triage", "intent": "gmail_triage",
        "primary_target": "Featured Company", "target_type": "company",
    }
    item = WorkItem(
        kind=WorkItemKind.GMAIL_THREAD, title="Newsletter",
        target=WorkItemTarget(metadata={"manual_request_plan": plan}),
    )
    expected = "Featured Company" if source == "orchestrator_canonical" else ""
    assert workflow._accepted_gmail_research_target(
        item, request=WorkflowRunRequest(manual_request_plan=plan),
    ) == expected
    assert workflow._accepted_gmail_research_target(
        item, request=WorkflowRunRequest(),
    ) == expected
