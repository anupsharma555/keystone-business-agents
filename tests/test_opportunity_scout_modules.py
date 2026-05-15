from __future__ import annotations

import json

from keystone_agents.opportunity_scout.scoring import (
    HANDOFF_PRIORITY_THRESHOLD,
    clean_signals,
    normalize_type,
    score_from_signals,
    should_handoff_to_business_research_analyst,
)
from keystone_agents.opportunity_scout.search_plan import (
    infer_opportunity_search_plan,
    merge_opportunity_search_plan,
)
from keystone_agents.opportunity_scout.state import (
    load_existing_state_map,
    normalize_company_key,
    normalize_pipeline_status,
)


def test_scoring_module_normalizes_signals_and_scores_handoff_ready_candidate() -> None:
    signals = clean_signals(["validation study", "validation study", "payer partnership"])
    breakdown = score_from_signals(signals, normalize_type("Behavioral Health AI"))

    assert signals == ["validation study", "payer partnership"]
    assert breakdown.priority_score >= HANDOFF_PRIORITY_THRESHOLD
    assert should_handoff_to_business_research_analyst(
        breakdown=breakdown,
        source_quality_summary=None,
        missing_evidence=[],
        contradictions=[],
        weak_evidence_reasons=[],
    )


def test_state_module_loads_existing_state_without_agent_facade() -> None:
    payload = {
        "states": [
            {
                "company_name": "Curebase, Inc.",
                "status": "approved-for-draft",
                "opportunity_type": "trial technology",
            }
        ]
    }

    states = load_existing_state_map(json.dumps(payload))

    assert normalize_company_key("Curebase, Inc.") == "curebase"
    assert normalize_pipeline_status("approved-for-draft") == "approved_for_drafting"
    assert states["curebase"].status == "approved_for_drafting"
    assert states["curebase"].opportunity_type == "trial technology"


def test_search_plan_infers_journal_and_contract_lanes_for_all_lane_loop() -> None:
    plan = infer_opportunity_search_plan(
        "Find opportunities across conferences, journal article calls, contracts, grants, "
        "trials, companies, researchers, and institutes.",
        desired_count=3,
    )

    assert "journal_call" in plan.target_entity_types
    assert "contract_rfp" in plan.target_entity_types
    assert "journal_article_call" in plan.objectives
    assert "contract_opportunity" in plan.objectives


def test_search_plan_treats_bounded_company_requests_as_strict_company_only() -> None:
    plan = infer_opportunity_search_plan(
        "Identify 5 mental health AI companies with possible clinical validation needs; "
        "no outreach.",
        desired_count=5,
    )

    assert plan.target_entity_types == ["company"]
    assert plan.strict_targeting is True
    assert {"institute", "grant_program", "trial", "researcher"} <= set(
        plan.exclude_entity_types
    )


def test_search_plan_merge_preserves_strict_company_only_contract() -> None:
    base = infer_opportunity_search_plan(
        "Find 5 mental health AI companies with possible clinical validation needs.",
        desired_count=5,
    )
    merged = merge_opportunity_search_plan(
        base,
        {
            "source": "llm",
            "desired_count": 5,
            "target_entity_types": ["company"],
            "objectives": ["broad_discovery"],
            "strict_targeting": False,
        },
    )

    assert merged.strict_targeting is True
    assert "institute" in merged.exclude_entity_types
