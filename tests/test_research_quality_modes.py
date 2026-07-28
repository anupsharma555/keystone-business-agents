from __future__ import annotations

from keystone_agents.quality_budget import (
    QualityMode,
    business_research_quality_budget,
    opportunity_scout_quality_budget,
)
from keystone_agents.schemas.manual_request_plan import (
    AskShapePolicy,
    ManualRequestPlan,
)


def _plan(*, ask_shape: AskShapePolicy, requires_live_search: bool = True) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="llm",
        target_agent="business_research_analyst",
        intent="research_brief",
        objective="Research the target using evidence.",
        task_objective="source_research",
        expected_artifact_type="source_summary",
        requires_live_search=requires_live_search,
        ask_shape=ask_shape,
    )


def test_canonical_deep_research_survives_conservative_transport_profile() -> None:
    plan = _plan(
        ask_shape=AskShapePolicy(
            ask_breadth="broad",
            evidence_depth="deep",
            cost_mode="quality",
        )
    )

    budget = business_research_quality_budget(
        request_text="Investigate the company.",
        live_search=True,
        cost_profile="slack_conservative",
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.DEEP
    assert budget.enable_page_verification is True
    assert budget.enable_synthesis_review is True
    assert budget.max_seconds == 300


def test_canonical_quick_narrow_minimize_plan_selects_fast_mode() -> None:
    plan = _plan(
        ask_shape=AskShapePolicy(
            ask_breadth="narrow",
            evidence_depth="quick",
            cost_mode="minimize",
        )
    )

    budget = business_research_quality_budget(
        request_text="Return a brief scan.",
        live_search=True,
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.FAST
    assert budget.hosted_web_search_max_calls == 0
    assert budget.enable_context_deepening is False


def test_canonical_standard_live_research_defaults_to_balanced() -> None:
    plan = _plan(ask_shape=AskShapePolicy(evidence_depth="standard"))

    budget = business_research_quality_budget(
        request_text="Research the target.",
        live_search=True,
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.BALANCED
    assert budget.enable_context_deepening is True
    assert budget.max_seconds == 120


def test_explicit_mode_remains_authoritative_over_canonical_plan() -> None:
    plan = _plan(
        ask_shape=AskShapePolicy(
            ask_breadth="broad",
            evidence_depth="deep",
            cost_mode="quality",
        )
    )

    budget = business_research_quality_budget(
        mode="fast",
        request_text="Research the target.",
        live_search=True,
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.FAST


def test_explicit_deep_research_wording_overrides_conservative_transport_profile() -> None:
    plan = ManualRequestPlan(
        source="heuristic",
        target_agent="business_research_analyst",
        intent="company_research",
        objective="Deeply research the anchor and its competitors.",
        task_objective="source_research",
        expected_artifact_type="research_brief",
        requires_live_search=True,
    )

    budget = business_research_quality_budget(
        request_text="Deeply research Northstar Health as the anchor.",
        live_search=False,
        cost_profile="slack_conservative",
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.DEEP
    assert budget.enable_page_verification is True
    assert budget.enable_synthesis_review is True
    assert budget.max_seconds == 300


def test_formal_opportunity_evidence_floor_still_elevates_to_deep() -> None:
    plan = _plan(
        ask_shape=AskShapePolicy(
            ask_breadth="narrow",
            evidence_depth="quick",
            cost_mode="minimize",
        )
    )

    budget = opportunity_scout_quality_budget(
        request_text="Assess the supplied opportunity.",
        live_search=True,
        formal_opportunity=True,
        manual_request_plan=plan,
    )

    assert budget.mode == QualityMode.DEEP
    assert budget.enable_page_verification is True
