"""Accepted preflight usage survives downstream CLI failures and copied results."""

from __future__ import annotations

import json

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from keystone_agents import cli
from keystone_agents.agents import orchestrator
from keystone_agents.model_provider import ModelConfig
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.runtime.durable_execution import ExecutionConflict
from keystone_agents.schemas.orchestrator import OrchestratorResult, OrchestratorRouteDecision
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.storage.sqlite_store import SQLiteStore


@pytest.mark.parametrize("fail_preparation", [True, False])
def test_cli_records_returned_preflight_before_work_item_and_counts_it_once(
    tmp_path, monkeypatch, capsys, fail_preparation
):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("KEYSTONE_TRACE_PROCESSOR", raising=False)
    monkeypatch.delenv("KEYSTONE_TRACE_SUMMARY_DB", raising=False)
    routes = ["business_research_analyst", "opportunity_scout"]
    model_output = OrchestratorResult(
        route=routes[0],
        target_agent=routes[0],
        workflow=routes,
        routing_mode="llm",
        decision=OrchestratorRouteDecision(
            decision_owner="orchestrator",
            decision_stage="orchestrator_route_selection",
            selected_candidate_id=routes[0],
            selected_candidate_ids=routes,
            candidate_assessments=[
                {
                    "candidate_id": route,
                    "disposition": "selected",
                    "rationale": "A requested stage.",
                }
                for route in routes
            ],
            reasoning="Preserve both requested synthetic stages.",
        ),
    ).model_dump(mode="json", exclude={"retrieval_diagnostics"})

    class PreflightModel(Model):
        calls = 0

        async def get_response(self, *args, **kwargs):
            self.calls += 1
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="synthetic-preflight-output",
                        type="message",
                        role="assistant",
                        status="completed",
                        content=[
                            ResponseOutputText(
                                type="output_text", text=json.dumps(model_output), annotations=[]
                            )
                        ],
                    )
                ],
                usage=Usage(requests=1, input_tokens=10, output_tokens=20, total_tokens=30),
                response_id="synthetic-preflight-response",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    model = PreflightModel()

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    original_preflight = orchestrator.run_orchestrator_preflight
    original_sdk_run = orchestrator.run_typed_sdk_agent

    def local_preflight(request, **kwargs):
        return original_preflight(
            request,
            **{
                **kwargs,
                "live_orchestrator": False,
                "live_manual_plan": False,
                "run_config": build_local_run_config(Provider()),
            },
        )

    def priced_local_sdk(**kwargs):
        return original_sdk_run(
            **{**kwargs, "config": ModelConfig(provider="openai", model="gpt-5.4-mini")}
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", local_preflight)
    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", priced_local_sdk)
    database_url = f"sqlite:///{tmp_path / 'business.db'}"
    store = SQLiteStore(database_url)
    captured = {}
    preparation_error = ExecutionConflict("Synthetic preparation mismatch after preflight.")

    def graph_boundary(request, **kwargs):
        scope = cli._ASK_ENTRY_TELEMETRY.get()
        attempt = store.get_agent_run(scope.execution_attempt.run_id)
        assert attempt["status"] == "started"
        observation = attempt["output"]["orchestrator_preflight_observation"]
        assert observation["capture_stage"] == "orchestrator_returned_before_cli_refinement"
        assert observation["sdk_usage_events"][0]["usage"]["requests"] == model.calls == 1
        assert attempt["output"]["request_budget"]["consumed"] == 1
        reference = observation["plan_reference"]
        accepted = scope.durable_store.stage_result(
            reference["execution_id"], reference["stage_id"], {}
        )
        assert accepted["capture_stage"] == "before_cli_plan_refinement"
        assert accepted["preflight"]["route_result"]["workflow"] == routes
        assert accepted["execution_reuse_authorized"] is False
        dispatched = scope.durable_store.stage_result(
            scope.durable_execution_id, "work_item_dispatch_request", {}
        )
        assert dispatched["capture_stage"] == "after_cli_refinement_before_work_item_execution"
        assert dispatched["execution_reuse_authorized"] is False
        captured["attempt_id"] = attempt["id"]
        if fail_preparation:
            raise preparation_error
        # A deterministic fixture stage adds no model call, but copies preflight
        # metadata just as a later successful linked SDK result does.
        linked_id = store.save_agent_run(
            agent_name="business_research_analyst",
            status="success",
            output={
                "usage": {
                    "available": True,
                    "requests": 0,
                    "total_tokens": 0,
                    "provider_request_count_confirmed": True,
                },
                "cost": {"estimated_usd": 0},
                "orchestrator_preflight": request.orchestrator_preflight,
            },
        )
        cli._register_entry_agent_run(linked_id)
        item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic completed fixture")
        store.save_work_item(item)
        return WorkflowRunResult(
            work_item=item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=item.status,
            advanced=True,
            human_summary="Synthetic fixture completed.",
        )

    monkeypatch.setattr(
        "keystone_agents.langgraph_workflow.advance_work_item_manager_loop_with_optional_langgraph",
        graph_boundary,
    )
    result = cli.main(
        [
            "ask",
            "--agent",
            "orchestrator",
            "--no-live-sdk",
            "--json",
            "--database-url",
            database_url,
            "--max-openai-requests",
            "2",
            "Research Example Analytics and identify potential collaboration opportunities.",
        ]
    )
    streams = capsys.readouterr()
    assert result == (1 if fail_preparation else 0)
    assert model.calls == 1 and captured
    row = store.get_agent_run(captured["attempt_id"])
    audit = row["output"]
    budget = audit["request_budget"]
    assert budget["consumed"] == budget["observed_model_requests"] == 1
    assert budget["observed_count_confirmed"] is True
    assert budget["usage_reconciled_with_ledger"] is True
    assert budget["limit"] == 2 and budget["remaining"] == 1
    stages = audit["decision_trace"]["stages"]
    assert sum(stage["consumption"]["model_request_count"] for stage in stages) == 1
    assert sum(stage["consumption"]["usage"].get("total_tokens", 0) for stage in stages) == 30
    assert sum(
        stage["consumption"]["cost"].get("estimated_usd", 0) for stage in stages
    ) == pytest.approx(0.0000975)
    assert audit["orchestrator_preflight_observation"]["plan_reference"]
    if fail_preparation:
        assert row["status"] == audit["terminal"]["status"] == "failed"
        assert audit["internal_diagnostics"]["error_type"] == "ExecutionConflict"
        assert (
            audit["failure"]["kind"] == known_exception_to_operator_failure(preparation_error).kind
        )
        assert json.loads(streams.out)["status"] == "failed"
        assert streams.err.count("Business Agents run failed:") == 1
        assert "Business Agents run failed with structured diagnostics." not in streams.err
        assert audit["links"]["work_item_id"] == ""
    else:
        assert row["status"] == "completed"
        assert len(audit["links"]["agent_run_ids"]) == 1
