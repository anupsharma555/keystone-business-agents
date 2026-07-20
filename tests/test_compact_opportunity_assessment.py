from __future__ import annotations

from types import SimpleNamespace

import pytest

from keystone_agents.agents.opportunity_scout import build_opportunity_assessment_agent
from keystone_agents.schemas.opportunity import OpportunityAssessmentBrief
from scripts.run_compact_opportunity_assessment import (
    compact_input,
    compact_opportunity_human_summary,
    inline_source_packet,
    pilot_ask,
    validate_payload,
)
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
    instructions = str(agent.instructions)

    assert agent.output_type is OpportunityAssessmentBrief
    assert agent.tools == []
    assert "<!-- opportunity_assessment_compact.md -->" in instructions
    assert "<!-- opportunity_scout.md -->" not in instructions
    assert "<!-- agent-operating-architecture.md -->" not in instructions
    assert "<!-- slack-posting-rules.md -->" not in instructions
    assert "<!-- memory_policy.md -->" in instructions
    assert "<!-- writing_style.md -->" in instructions
    assert len(instructions) < 40_000


def test_compact_input_uses_exact_pilot_ask_and_supplied_packet() -> None:
    typed_input = compact_input(_load_packet(SOURCE_PACKET))

    assert typed_input.topic == pilot_ask()
    assert "avoid_duplicate_source_or_pipeline_layers" in typed_input.context


def test_compact_input_accepts_exact_inline_request() -> None:
    request = "Assess the supplied internal pilot context without external research."

    typed_input = compact_input(
        inline_source_packet(request, "Example Health is planning an internal pilot."),
        request_text=request,
    )

    assert typed_input.topic == request
    assert "operator_provided_only" in typed_input.context


@pytest.mark.parametrize(
    ("context", "expected_url"),
    [
        (
            "Example Health posted details at https://example.test/pilot.",
            "https://example.test/pilot",
        ),
        ("Example Health is planning an internal pilot.", "operator://inline-opportunity-context"),
    ],
)
def test_inline_source_packet_retains_operator_evidence(
    context: str,
    expected_url: str,
) -> None:
    packet = inline_source_packet("Assess this opportunity.", context)

    assert packet["source_scope"] == "operator_provided_only"
    assert packet["external_verification_performed"] is False
    assert packet["sources"][0]["url"] == expected_url
    assert packet["sources"][0]["facts"] == [context]


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
    assert payload["human_summary"] == payload["slack_display_text"]
    assert "*Review notes:*" not in payload["human_summary"]


def test_compact_public_summary_contains_decision_without_run_metadata() -> None:
    summary = compact_opportunity_human_summary(_brief())

    assert "What the supplied context establishes:" in summary
    assert "What it does not establish:" in summary
    assert "Most credible KNI opportunity:" in summary
    assert "Single most important validation gap:" in summary
    assert "provider" not in summary.lower()
    assert "run_id" not in summary
    assert "search:" not in summary.lower()


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


def test_generic_inline_receipt_uses_source_and_safety_contract_only() -> None:
    brief = _brief()
    brief.retained_sources[0].source_id = "operator:inline-opportunity-context:1"
    brief.retained_sources[0].url = "operator://inline-opportunity-context"
    brief.retained_sources = brief.retained_sources[:1]
    brief.confirmed_facts[0].source_ids = [brief.retained_sources[0].source_id]
    result = SimpleNamespace(
        final_output=brief,
        usage={"requests": 1},
        cost={"estimated_usd": 0.01},
        request_cache={"rate_limit_retries": 0},
    )

    payload = validate_payload(result, budget_usd=0.10, pilot_contract=False)

    assert payload["status"] == "pass"
    assert "exact_sources_retained_once" not in payload["checks"]
    assert payload["checks"]["retained_source_identity_present"] is True
