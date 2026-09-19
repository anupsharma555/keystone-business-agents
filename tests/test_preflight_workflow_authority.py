"""Accepted Orchestrator routing must survive downstream admission unchanged."""

import pytest

from keystone_agents.agents import manual_request_planner, orchestrator
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.entrypoints import cli_impl
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorResult


@pytest.mark.parametrize("source", ["heuristic", "llm"])
def test_accepted_workflow_reorders_stages_without_promoting_provider_authority(
    monkeypatch, source
) -> None:
    plan = ManualRequestPlan(
        source=source,
        target_agent="business_research_analyst",
        workflow=["business_research_analyst", "opportunity_scout"],
        provider_system="gmail",
        provider_operations=["read"],
        ask_shape={"permission_state": "read_only"},
    )
    candidate = OrchestratorResult(
        route="opportunity_scout",
        workflow=["opportunity_scout", "business_research_analyst"],
        rationale="Assess candidate opportunities before researching the selected source.",
    )
    monkeypatch.setattr(manual_request_planner, "resolve_manual_request_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        orchestrator,
        "run_typed_sdk_agent",
        lambda **k: TypedAgentRunResult(
            agent_name="orchestrator", output=candidate, raw_result=None, live=False
        ),
    )

    preflight = orchestrator.run_orchestrator_preflight(
        "Assess the opportunities, then research the selected source.",
        workflow_state={},
        run_config=object(),
    )

    assert preflight.manual_request_plan.workflow == candidate.workflow
    assert cli_impl._preflight_workflow_routes(preflight) == candidate.workflow
    assert cli_impl._preflight_work_item_entry_route(preflight) == "opportunity_scout"
    assert preflight.manual_request_plan.source == source
    assert preflight.manual_request_plan.provider_operations == ["read"]
    assert preflight.manual_request_plan.ask_shape.permission_state == "read_only"
    assert ExecutionIntentAuthority.from_value(preflight.manual_request_plan).canonical == (
        source == "llm"
    )


def test_accepted_single_owner_does_not_retain_heuristic_extra_stages(monkeypatch) -> None:
    plan = ManualRequestPlan(
        target_agent="business_research_analyst",
        workflow=["business_research_analyst", "opportunity_scout"],
    )
    candidate = OrchestratorResult(route="chief_of_staff", workflow=[])
    monkeypatch.setattr(manual_request_planner, "resolve_manual_request_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        orchestrator,
        "run_typed_sdk_agent",
        lambda **k: TypedAgentRunResult(
            agent_name="orchestrator", output=candidate, raw_result=None, live=False
        ),
    )

    preflight = orchestrator.run_orchestrator_preflight(
        "Summarize the supplied project note only.", workflow_state={}, run_config=object()
    )

    assert preflight.manual_request_plan.target_agent == "chief_of_staff"
    assert cli_impl._preflight_workflow_routes(preflight) == []
    assert cli_impl._bounded_direct_route_from_preflight(preflight) == "chief_of_staff"
    assert not cli_impl._preflight_requires_work_item(preflight)


def test_read_only_heuristic_plan_stays_non_authoritative_after_validated_routing(monkeypatch):
    plan = ManualRequestPlan(
        target_agent="gmail_triage", provider_system="gmail", provider_operations=["read"]
    )
    candidate = OrchestratorResult(route="gmail_triage", workflow=["gmail_triage"])
    monkeypatch.setattr(manual_request_planner, "resolve_manual_request_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        orchestrator,
        "run_typed_sdk_agent",
        lambda **k: TypedAgentRunResult(
            agent_name="orchestrator", output=candidate, raw_result=None, live=False
        ),
    )

    preflight = orchestrator.run_orchestrator_preflight(
        "Read the current Gmail context.", workflow_state={}, run_config=object()
    )

    assert not ExecutionIntentAuthority.from_value(preflight.manual_request_plan).canonical
    assert preflight.manual_request_plan.provider_operations == ["read"]
