from __future__ import annotations

from types import SimpleNamespace

import pytest

from keystone_agents.agents.opportunity_scout import build_opportunity_assessment_agent
from keystone_agents.schemas.opportunity import OpportunityAssessmentBrief
from scripts.run_compact_opportunity_assessment import compact_input, pilot_ask, validate_payload
from scripts.run_opportunity_normalization_validation import SOURCE_PACKET, _load_packet


def _brief() -> OpportunityAssessmentBrief:
    packet = _load_packet(SOURCE_PACKET)
    sources = [
        {
            "source_id": source["source_id"],
            "title": source["title"],
            "url": source["url"],
            "source_type": "fixture",
            "supported_signal": source["facts"][0],
        }
        for source in packet["sources"]
    ]
    return OpportunityAssessmentBrief.model_validate(
        {
            "opportunity_name": "Hack for Humanity | Summer 2026",
            "opportunity_type": "hackathon or challenge opportunity",
            "confirmed_facts": [
                {
                    "statement": "The submission window starts August 7, 2026.",
                    "source_ids": [sources[1]["source_id"]],
                }
            ],
            "interpretation": "This is a prototype venue, not a consulting lead.",
            "keystone_fit": "Conditional fit for a clinical-AI portfolio artifact.",
            "timing_status": "Upcoming; not yet open.",
            "geography_status": "Unknown from the supplied pages.",
            "missing_evidence": ["Participant eligibility is unknown."],
            "next_safe_action": "Conditionally verify eligibility before prototype work.",
            "retained_sources": sources,
        }
    )


def test_compact_agent_has_no_tools_and_compact_output_schema() -> None:
    agent = build_opportunity_assessment_agent(request_text=pilot_ask())

    assert agent.output_type is OpportunityAssessmentBrief
    assert agent.tools == []


def test_compact_input_uses_exact_pilot_ask_and_supplied_packet() -> None:
    typed_input = compact_input(_load_packet(SOURCE_PACKET))

    assert typed_input.topic == pilot_ask()
    assert "avoid_duplicate_source_or_pipeline_layers" in typed_input.context


def test_compact_schema_rejects_unknown_fact_sources_or_side_effects() -> None:
    payload = _brief().model_dump(mode="json")
    payload["confirmed_facts"][0]["source_ids"] = ["unknown"]
    with pytest.raises(ValueError, match="retained source"):
        OpportunityAssessmentBrief.model_validate(payload)

    payload = _brief().model_dump(mode="json")
    payload["outreach_recommended"] = True
    with pytest.raises(ValueError, match="review-only"):
        OpportunityAssessmentBrief.model_validate(payload)


def test_compact_receipt_proves_precision_and_safety() -> None:
    result = SimpleNamespace(
        final_output=_brief(),
        usage={"requests": 1, "output_tokens": 900},
        cost={"estimated_usd": 0.01},
        request_cache={"rate_limit_retries": 0},
    )

    payload = validate_payload(result, budget_usd=0.10)

    assert payload["status"] == "pass"
    assert all(payload["checks"].values())
    assert payload["safety"]["provider_writes"] == 0


def test_geography_uncertainty_does_not_require_literal_unknown() -> None:
    brief = _brief()
    brief.geography_status = "Geographic eligibility was not established by the sources."
    result = SimpleNamespace(
        final_output=brief,
        usage={"requests": 1},
        cost={"estimated_usd": 0.01},
        request_cache={"rate_limit_retries": 0},
    )

    payload = validate_payload(result, budget_usd=0.10)

    assert payload["checks"]["unknown_geography_retained"] is True
