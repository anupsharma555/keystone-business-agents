from __future__ import annotations

from types import SimpleNamespace

import pytest

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _args(*, live_search: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        context_file="",
        agent=None,
        max_manager_steps=3,
        live_search=live_search,
    )


def _plan(
    route: str,
    *,
    intent: str = "context_lookup",
    task_objective: str = "context_lookup",
    requires_live_search: bool = False,
    provider_system: str = "unspecified",
    provider_operations: list[str] | None = None,
    workflow: list[str] | None = None,
) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="canonical:test",
        requested_agent=route,
        target_agent=route,
        intent=intent,
        task_objective=task_objective,
        requires_live_search=requires_live_search,
        provider_system=provider_system,
        provider_operations=provider_operations or [],
        workflow=workflow or [],
    )


@pytest.mark.parametrize(
    "route",
    ["business_research_analyst", "opportunity_scout"],
)
def test_research_routes_reserve_tool_correction_and_decision_repair(route: str) -> None:
    request = "Find and compare two current source-backed candidates."
    plan = _plan(
        route,
        intent="company_research" if route == "business_research_analyst" else "opportunity_search",
        task_objective=(
            "entity_research"
            if route == "business_research_analyst"
            else "opportunity_discovery"
        ),
        requires_live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        _args(live_search=True),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route=route,
        manual_plan=plan,
        effective_live_search=True,
        observed_orchestrator_requests=0,
    )

    route_rows = [row for row in estimate["stage_rows"] if row["route"] == route]
    initial = cli._direct_specialist_request_estimate(
        route,
        input_text=request,
        live_search=True,
        manual_plan=plan,
    )
    expected_caps = (
        [initial, 2, 1]
        if route == "opportunity_scout"
        else [initial, initial, initial]
    )
    assert [row["max_requests"] for row in route_rows] == expected_caps
    assert estimate["max"] == sum(expected_caps)


def test_bounded_opportunity_reserves_the_full_recovery_envelope() -> None:
    request = (
        "Could you find one currently open U.S. accelerator cohort, grant, or pilot "
        "program that a small neuroinformatics consultancy could apply to before "
        "November 1? Pick the best one and include the official link and caveat."
    )
    plan = _plan(
        "opportunity_scout",
        intent="opportunity_search",
        task_objective="opportunity_discovery",
        requires_live_search=True,
    )

    estimate = cli._estimate_ask_openai_requests(
        _args(live_search=True),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="opportunity_scout",
        manual_plan=plan,
        effective_live_search=True,
        observed_orchestrator_requests=1,
    )

    route_rows = [
        row for row in estimate["stage_rows"] if row["route"] == "opportunity_scout"
    ]
    assert [row["max_requests"] for row in route_rows] == [4, 2, 1]
    assert [row["admission_reserve_requests"] for row in route_rows] == [4, 2, 1]
    assert cli._post_preflight_admission_reserve(estimate) == 7
    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate,
        requested_limit=8,
        openai_requests_made=1,
    ) is False
    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate,
        requested_limit=7,
        openai_requests_made=1,
    ) is True


def test_natural_one_result_opportunity_keeps_compact_stage_cap_with_deep_tool_ceiling(
) -> None:
    request = (
        "Opportunity Scout, find current U.S.-based accelerators, grants, or pilot "
        "programs that a small neuroinformatics consultancy could pursue for "
        "behavioral-health measurement work. Compare the plausible choices, return "
        "one best fit with source URLs and eligibility caveats, and don't save "
        "anything or draft outreach."
    )
    plan = infer_manual_request_plan(request, requested_agent="opportunity_scout")

    profile = cli._direct_specialist_runtime_profile(
        "opportunity_scout",
        input_text=request,
        manual_plan=plan,
    )

    assert profile["compact_instructions"] is True
    assert profile["deep_request"] is False
    assert cli._direct_specialist_request_estimate(
        "opportunity_scout",
        input_text=request,
        live_search=True,
        manual_plan=plan,
    ) == 4


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_direct_business_system_writes_reserve_a_terminal_synthesis_turn(
    route: str,
) -> None:
    request = "Update one exact provider record and verify the same record."
    plan = _plan(
        route,
        intent="business_system_write",
        task_objective="business_system_write",
    )

    assert cli._direct_specialist_request_estimate(
        route,
        input_text=request,
        live_search=False,
        manual_plan=plan,
    ) == 4


def test_deep_research_uses_full_quality_turn_policy_for_every_semantic_attempt() -> None:
    request = "Run a comprehensive multi-stage current evidence review across sources."
    plan = _plan(
        "business_research_analyst",
        intent="company_research",
        task_objective="entity_research",
        requires_live_search=True,
    )
    plan.desired_count = 8
    plan.desired_count_explicit = True
    plan.ask_shape = plan.ask_shape.model_copy(
        update={"ask_breadth": "broad", "evidence_depth": "deep"}
    )

    estimate = cli._estimate_ask_openai_requests(
        _args(live_search=True),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="business_research_analyst",
        manual_plan=plan,
        effective_live_search=True,
        observed_orchestrator_requests=0,
    )

    rows = [
        row
        for row in estimate["stage_rows"]
        if row["route"] == "business_research_analyst"
    ]
    assert [row["max_requests"] for row in rows] == [12, 12, 12]
    assert estimate["max"] == 36


def test_gmail_selection_and_verified_continuation_have_distinct_evidence_gated_caps() -> None:
    request = "Find the current invitation, compare plausible threads, and draft Slack copy."
    plan = _plan(
        "gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        provider_system="gmail",
        provider_operations=["search", "read"],
    )

    selection = cli._estimate_ask_openai_requests(
        _args(),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="gmail_triage",
        manual_plan=plan,
        observed_orchestrator_requests=0,
    )
    continuation = cli._estimate_ask_openai_requests(
        _args(),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="gmail_triage",
        manual_plan=plan,
        observed_orchestrator_requests=0,
        verified_gmail_continuation=True,
    )

    assert [row["max_requests"] for row in selection["stage_rows"]] == [0, 7, 7, 2]
    assert [row["min_requests"] for row in selection["stage_rows"]] == [0, 3, 0, 0]
    assert cli._mandatory_post_preflight_request_minimum(selection) == 3
    assert selection["mandatory_request_minimum"] == 3
    assert selection["max"] == 16
    assert [row["max_requests"] for row in continuation["stage_rows"]] == [0, 4, 4, 2]
    assert [row["min_requests"] for row in continuation["stage_rows"]] == [0, 2, 0, 0]
    assert cli._mandatory_post_preflight_request_minimum(continuation) == 2
    assert continuation["mandatory_request_minimum"] == 2
    assert continuation["max"] == 10


def test_gmail_specialist_minimum_survives_orchestrator_preflight_budget() -> None:
    request = (
        "Find the current interview thread, compare the plausible emails, "
        "and draft Slack copy."
    )
    plan = _plan(
        "gmail_triage",
        intent="gmail_triage",
        task_objective="gmail_triage",
        provider_system="gmail",
        provider_operations=["search", "read"],
    )
    estimate = cli._estimate_ask_openai_requests(
        _args(),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="gmail_triage",
        manual_plan=plan,
    )

    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate,
        requested_limit=4,
        openai_requests_made=2,
    ) is True
    assert cli._ask_request_estimate_exceeds_ceiling(
        estimate,
        requested_limit=5,
        openai_requests_made=2,
    ) is False


def test_gmail_schema_path_reserves_only_schema_tool_correction() -> None:
    request = "List the available Gmail fields but do not read mailbox messages."
    plan = _plan(
        "gmail_triage",
        intent="context_lookup",
        provider_system="gmail",
        provider_operations=["schema"],
    )

    estimate = cli._estimate_ask_openai_requests(
        _args(),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="gmail_triage",
        manual_plan=plan,
        observed_orchestrator_requests=0,
    )

    assert [(row["stage"], row["max_requests"]) for row in estimate["stage_rows"]] == [
        ("orchestrator_preflight", 0),
        ("gmail_triage_direct_sdk", 2),
        ("conditional_gmail_triage_tool_correction", 2),
    ]
    assert estimate["max"] == 4


@pytest.mark.parametrize("route", ["rss_context_agent", "preprints_context_agent"])
def test_signal_routes_reserve_history_correction_and_outer_decision_repair(
    route: str,
) -> None:
    request = "Review the recent signals and select the relevant items without mutation."
    plan = _plan(route)

    estimate = cli._estimate_ask_openai_requests(
        _args(),
        input_text=request,
        live_sdk=True,
        live_manual_plan=False,
        requested_route=route,
        manual_plan=plan,
        observed_orchestrator_requests=0,
    )

    route_rows = [row for row in estimate["stage_rows"] if row["route"] == route]
    assert [row["max_requests"] for row in route_rows] == [3, 3, 3]
    assert estimate["max"] == 9


def test_chief_reserves_only_canonically_admitted_nested_specialists() -> None:
    nested = _plan("chief_of_staff", workflow=["airtable_context_agent"])
    single_owner = _plan("chief_of_staff")

    nested_estimate = cli._estimate_ask_openai_requests(
        _args(),
        input_text="Coordinate one Airtable context review and summarize the result.",
        live_sdk=True,
        live_manual_plan=False,
        requested_route="chief_of_staff",
        manual_plan=nested,
        observed_orchestrator_requests=0,
    )
    single_estimate = cli._estimate_ask_openai_requests(
        _args(),
        input_text="Summarize the supplied operating note without provider tools.",
        live_sdk=True,
        live_manual_plan=False,
        requested_route="chief_of_staff",
        manual_plan=single_owner,
        observed_orchestrator_requests=0,
    )

    nested_stages = [row["stage"] for row in nested_estimate["stage_rows"]]
    assert "chief_nested_airtable_context_agent_sdk" in nested_stages
    assert "conditional_chief_nested_airtable_context_agent_tool_correction" in nested_stages
    assert "conditional_chief_nested_airtable_context_agent_decision_repair" in nested_stages
    assert not any("gmail_triage" in stage for stage in nested_stages)
    assert not any("chief_nested_" in row["stage"] for row in single_estimate["stage_rows"])
    assert "transport retries remain outside" in nested_estimate["note"].lower()
