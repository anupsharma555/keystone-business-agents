from __future__ import annotations

import json

from keystone_agents.opportunity_scout.scoring import (
    HANDOFF_PRIORITY_THRESHOLD,
    clean_signals,
    normalize_type,
    score_from_signals,
    should_handoff_to_business_research_analyst,
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
