"""Execution failures and genuine context requests have distinct durable public results."""

from __future__ import annotations

import json

import httpx
import pytest
from agents.exceptions import ModelBehaviorError
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai import APIConnectionError, RateLimitError
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import sdk
from keystone_agents import workflow_runner as workflow
from keystone_agents.agents.gmail_triage import triage_email_fixture
from keystone_agents.costing import AgentRunBudgetExceededError
from keystone_agents.model_provider import MissingOpenAIAPIKeyError, ModelProviderConfigurationError
from keystone_agents.presentation.public_result import attach_execution_public_result
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore

CANARY = "PRIVATE_ERROR_SOURCE_JSON_SEARCH_MUST_NOT_LEAK"


class ScriptedModel(Model):
    def __init__(self, output="{"):
        self.output = output
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="failure-control-message",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(type="output_text", text=self.output, annotations=[])
                    ],
                )
            ],
            usage=Usage(requests=1, input_tokens=50, output_tokens=20, total_tokens=70),
            response_id="failure-control-response",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


def case(tmp_path, owner):
    route = WorkItemRoute(owner)
    request_text = (
        "Review the supplied evidence only. Email: From: Sender <sender@example.test>. "
        "Subject: Synthetic pilot. Body: The supplied proposal describes an evaluation pilot."
    )
    plan = {
        "source": "orchestrator_canonical",
        "target_agent": owner,
        "intent": {
            "gmail_triage": "gmail_triage",
            "business_research_analyst": "research_brief",
            "opportunity_scout": "opportunity_search",
        }[owner],
        "primary_target": "Synthetic Company",
        "target_type": "company",
    }
    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Complete supplied input",
        request_text=request_text,
        current_route=route,
        target=WorkItemTarget(
            name="Synthetic Company",
            object_type="company",
            metadata={
                "manual_request_plan": plan,
                "external_context": {
                    "schema": "keystone.work_item.source_bundle.v1",
                    "supplied_material_only": True,
                },
            },
        ),
        sources=[
            WorkItemSourceRef(
                source_id="supplied-evidence-1",
                provider_candidate_id="supplied-evidence-1",
                title="Supplied evidence",
                url="https://example.test/pilot",
                provider="operator_supplied",
                extraction_status="supplied_material",
                evidence_excerpt="The supplied proposal describes an evaluation pilot.",
            )
        ],
        approval_gates=[WorkItemApprovalGate(scope="external_use", state="pending", required=True)],
    )
    database_url = f"sqlite:///{tmp_path / 'failure.db'}"
    store = SQLiteStore(database_url)
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
        manual_request_plan=plan,
    )
    prepared = workflow.PreparedWorkItemStep(
        request=request,
        work_item=item,
        route=route,
        input_text=request_text,
        context_pack={},
    )
    return store, prepared


def install_model(monkeypatch, owner, model):
    name = {
        "gmail_triage": "run_gmail_triage_sdk",
        "business_research_analyst": "run_business_research_analyst_research_brief_sdk",
        "opportunity_scout": "run_opportunity_scout_sdk",
    }[owner]
    actual = getattr(workflow, name)

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        return actual(
            typed_input,
            **{
                **kwargs,
                "run_config": build_local_run_config(Provider(model)),
            },
        )

    monkeypatch.setattr(workflow, name, invoke)


@pytest.mark.parametrize(
    "owner", ["business_research_analyst", "opportunity_scout", "gmail_triage"]
)
@pytest.mark.parametrize(
    "error,kind",
    [
        ("runtime", "unknown_error"),
        ("credentials", "missing_credentials"),
        ("connection", "apiconnectionerror"),
        ("budget", "model_request_budget_exhausted"),
        ("cost", "agentrunbudgetexceedederror"),
        ("configuration", "modelproviderconfigurationerror"),
        ("rate_limit", "provider_rate_limit"),
        ("schema", "schema_or_parse_error"),
    ],
)
def test_complete_inputs_do_not_become_missing_context_after_execution_failure(
    tmp_path,
    monkeypatch,
    owner,
    error,
    kind,
):
    store, prepared = case(tmp_path, owner)
    model = ScriptedModel(json.dumps({"decision": CANARY}) if error == "schema" else "{")
    install_model(monkeypatch, owner, model)
    if error not in {"schema", "budget"}:
        failures = {
            "runtime": RuntimeError(f"supplied-source probe {CANARY}"),
            "credentials": MissingOpenAIAPIKeyError(CANARY),
            "connection": APIConnectionError(
                message=CANARY, request=httpx.Request("POST", "https://example.test")
            ),
            "cost": AgentRunBudgetExceededError(CANARY),
            "configuration": ModelProviderConfigurationError(CANARY),
            "rate_limit": RateLimitError(
                f"{CANARY} credential timeout source",
                response=httpx.Response(429, request=httpx.Request("POST", "https://example.test")),
                body=None,
            ),
        }

        async def stop_before_request(*args, **kwargs):
            raise failures[error]

        monkeypatch.setattr(sdk._ExecutionBoundaryRunHooks, "on_llm_start", stop_before_request)
    with activate_model_request_budget(0 if error == "budget" else 2) as budget:
        result = workflow.run_prepared_work_item_specialist(prepared)
        result = workflow.finalize_prepared_work_item_step(
            prepared,
            result,
            synthesize_user_response=True,
        )
        assert budget.consumed == (1 if error == "schema" else 0)
    assert model.calls == (1 if error == "schema" else 0)
    assert result.status == WorkItemStatus.BLOCKED and not result.advanced
    assert result.artifact_refs == []
    assert not any(
        phrase in result.human_summary.lower()
        for phrase in (
            "needs one more input",
            "missing context",
            "approve",
            "gather it",
        )
    )
    restored = SQLiteStore(prepared.request.database_url).get_work_item(prepared.work_item.id)
    assert restored.sources == prepared.work_item.sources
    assert restored.approval_gates == prepared.work_item.approval_gates
    assert restored.blockers and restored.artifact_refs == []
    events = store.list_work_item_events(restored.id)
    blocked = next(event for event in reversed(events) if event.event_type == "advance_blocked")
    assert blocked.metadata["operator_failure"]["kind"] == kind
    assert blocked.metadata["operator_failure"]["reason"] == ""
    usage = [event for event in events if event.event_type == "workflow_sdk_usage"]
    assert len(usage) == 1
    assert sum(int(event.metadata.get("usage", {}).get("requests", 0)) for event in usage) == (
        1 if error == "schema" else 0
    )
    payload = result.model_dump(mode="json")
    public = attach_execution_public_result(payload)
    assert public.status in {"blocked", "failed"}
    assert public.completion_confirmed is False
    retained = json.dumps(
        {
            "public": payload,
            "events": [event.model_dump() for event in events],
            "rows": store.fetch_all("agent_runs"),
        },
        default=str,
    )
    assert CANARY not in retained


@pytest.mark.parametrize("owner", ["business_research_analyst", "opportunity_scout"])
def test_genuinely_missing_evidence_uses_existing_readiness_gate(tmp_path, monkeypatch, owner):
    store, prepared = case(tmp_path, owner)
    # No labeled inline evidence is provided in this missing-input control.
    prepared = workflow.PreparedWorkItemStep(
        request=prepared.request.model_copy(update={"request_text": "Assess the company."}),
        work_item=prepared.work_item.model_copy(
            update={"sources": [], "request_text": "Assess the company."}
        ),
        route=prepared.route,
        input_text="Assess the company.",
        context_pack={},
    )
    model = ScriptedModel()
    install_model(monkeypatch, owner, model)
    if owner == "business_research_analyst":
        result = workflow._supplied_business_research_sdk_result(
            prepared.work_item,
            request=prepared.request,
            store=store,
        )
    else:
        result = workflow.run_prepared_work_item_specialist(prepared)
    result = workflow.finalize_prepared_work_item_step(
        prepared, result, synthesize_user_response=True
    )
    assert model.calls == 0 and not result.advanced
    assert "source_sufficiency_required" in {blocker.code for blocker in result.blockers}
    assert "needs one more input" in result.human_summary
    assert store.get_work_item(prepared.work_item.id).blockers


def test_supplied_gmail_explicit_context_request_preserves_usage_without_handoff(
    tmp_path, monkeypatch
):
    store, prepared = case(tmp_path, "gmail_triage")
    message = workflow._inline_gmail_fixture_from_request(prepared.request.request_text)
    output = triage_email_fixture(message).model_copy(
        update={
            "summary": "Please clarify the intended assessment before I recommend next steps.",
            "message_id": "",
            "thread_id": "",
            "sender_email": "",
            "decision": triage_email_fixture(message).decision.model_copy(
                update={"needs_more_context": True}
            ),
            "recommended_next_agent": "business_research_analyst",
        }
    )
    model = ScriptedModel(output.model_dump_json())
    install_model(monkeypatch, "gmail_triage", model)
    with activate_model_request_budget(2) as budget:
        result = workflow.run_prepared_work_item_specialist(prepared)
        result = workflow.finalize_prepared_work_item_step(
            prepared, result, synthesize_user_response=True
        )
        assert budget.consumed == model.calls == 1
    assert not result.advanced and result.artifact_refs == []
    assert result.next_action.agent == WorkItemRoute.GMAIL_TRIAGE
    assert "gmail_agent_needs_more_context" in {blocker.code for blocker in result.blockers}
    assert result.human_summary == output.summary
    restored = store.get_work_item(prepared.work_item.id)
    assert restored.sources == prepared.work_item.sources
    assert restored.approval_gates == prepared.work_item.approval_gates
    assert restored.artifact_refs == []
    rows = store.fetch_all("agent_runs")
    assert len(rows) == 1 and rows[0]["status"] == "blocked"
    saved_output = json.loads(rows[0]["output_json"])
    assert saved_output["_sdk_usage"]["requests"] == 1
    assert saved_output["draft_created"] is False and saved_output["send_enabled"] is False


@pytest.mark.parametrize(
    "kind",
    [
        "model_output_limit_reached",
        "model_response_incomplete",
        "model_response_failed",
    ],
)
def test_terminal_response_failure_persists_as_execution_failure_without_more_inference(
    tmp_path, kind
):
    store, prepared = case(tmp_path, "opportunity_scout")
    exc = ModelBehaviorError(CANARY)
    exc.keystone_sdk_run_failure = {"failure_kind": kind}
    with activate_model_request_budget(0) as budget:
        result = workflow._blocked_work_item_specialist_execution(
            prepared.work_item,
            route=prepared.route,
            store=store,
            exc=exc,
        )
        result = workflow.finalize_prepared_work_item_step(
            prepared,
            result,
            synthesize_user_response=True,
        )
        assert budget.consumed == 0
    assert result.blockers[0].code == "opportunity_scout_execution_failed"
    payload = result.model_dump(mode="json")
    assert attach_execution_public_result(payload).status == "blocked"
    restored = store.get_work_item(prepared.work_item.id)
    assert restored.sources == prepared.work_item.sources
    assert restored.approval_gates == prepared.work_item.approval_gates
    blocked = next(
        event
        for event in store.list_work_item_events(restored.id)
        if event.event_type == "advance_blocked"
    )
    assert blocked.metadata["operator_failure"]["kind"] == kind
    assert CANARY not in json.dumps(payload)
