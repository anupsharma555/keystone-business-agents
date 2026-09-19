"""Signal text must not become positive business evidence merely by being present."""

from __future__ import annotations

import json

import pytest

from keystone_agents.agents.opportunity_scout import (
    score_opportunity_impl,
    scout_opportunities_fixture,
)
from keystone_agents.opportunity_scout.scoring import (
    HANDOFF_PRIORITY_THRESHOLD,
    TARGET_TYPES,
    clean_signals,
    outside_consulting_likelihood,
    score_from_signals,
    should_handoff_to_business_research_analyst,
)
from keystone_agents.source_quality import SourceQualitySummary

CAUTIONS = [
    "No buyer need established",
    "Requires further validation",
    "Internal implementation only",
    "No confirmed consulting demand",
]


def _numeric(breakdown):
    return breakdown.model_dump(exclude={"rationale", "component_rationales"})


@pytest.mark.parametrize("opportunity_type", TARGET_TYPES)
def test_free_text_cautions_do_not_inflate_any_numeric_component(opportunity_type):
    empty = score_from_signals([], opportunity_type)
    cautions = score_from_signals(CAUTIONS, opportunity_type)

    assert _numeric(cautions) == _numeric(empty)
    assert outside_consulting_likelihood(
        cautions, signal_count=len(CAUTIONS), signals=CAUTIONS
    ) == (outside_consulting_likelihood(empty, signal_count=0, signals=[]))


def test_known_signals_keep_weight_without_duplicate_or_case_bonus():
    canonical = score_from_signals(["hiring AI", "validation study"], "clinical AI")
    variants = score_from_signals(
        [" Hiring AI ", "HIRING AI", "validation study", "Validation Study", *CAUTIONS],
        "clinical AI",
    )

    assert _numeric(variants) == _numeric(canonical)
    assert canonical.urgency_score == 81
    assert canonical.priority_score == 74
    assert canonical.priority_score >= HANDOFF_PRIORITY_THRESHOLD


@pytest.mark.parametrize(
    "text",
    ["no recent funding", "validation study not confirmed", "product launch?", "future signal"],
)
def test_substring_mentions_are_not_promoted_to_canonical_signals(text):
    assert _numeric(score_from_signals([text], "clinical AI")) == _numeric(
        score_from_signals([], "clinical AI")
    )


def test_tool_arithmetic_does_not_reward_caution_count_or_change_input_evidence():
    original = CAUTIONS.copy()
    packet = json.loads(score_opportunity_impl("Example Company", "behavioral health AI", original))
    baseline = json.loads(score_opportunity_impl("Example Company", "behavioral health AI", []))

    assert original == CAUTIONS
    assert clean_signals([*original, original[0]]) == CAUTIONS
    assert packet["priority_score"] == baseline["priority_score"] == 61
    assert packet["outside_consulting_likelihood"] == baseline["outside_consulting_likelihood"]
    assert packet["handoff_to_business_research_analyst"] is False
    assert "0 recognized weighted signals" in packet["score_rationale"]


def test_supplied_evidence_controls_consulting_bonus_over_legacy_count():
    breakdown = score_from_signals(["validation study", "payer partnership"], "clinical AI")
    assert outside_consulting_likelihood(breakdown, signal_count=100, signals=[]) == (
        outside_consulting_likelihood(breakdown, signal_count=0, signals=[])
    )


def test_source_confidence_and_independent_research_handoff_are_preserved():
    quality = SourceQualitySummary(
        source_count=2,
        independent_source_count=2,
        average_credibility_score=80,
        average_recency_score=80,
        average_relevance_score=80,
        overall_score=80,
        high_quality_source_count=2,
        low_quality_source_count=0,
        rationale="Synthetic verified provider quality.",
    )
    empty = score_from_signals([], "clinical AI", source_quality_summary=quality)
    cautions = score_from_signals(CAUTIONS, "clinical AI", source_quality_summary=quality)

    assert _numeric(cautions) == _numeric(empty)
    assert cautions.source_confidence_score == 80
    assert should_handoff_to_business_research_analyst(
        breakdown=cautions,
        source_quality_summary=quality,
        missing_evidence=[],
        contradictions=[],
        weak_evidence_reasons=[],
    )


def test_canonical_consulting_baseline_and_legacy_count_compatibility():
    signals = ["validation study", "payer partnership"]
    breakdown = score_from_signals(signals, "behavioral health AI")
    assert outside_consulting_likelihood(breakdown, signal_count=2, signals=signals) == 88
    assert outside_consulting_likelihood(breakdown, signal_count=2) == 88
    mixed = [*signals, *CAUTIONS, "Validation Study"]
    assert outside_consulting_likelihood(breakdown, signal_count=len(mixed), signals=mixed) == 88


def test_fixture_record_scoring_retains_cautions_without_counting_them(tmp_path):
    def result(signals):
        fixture = tmp_path / "signals.json"
        fixture.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "company_name": "Example Company",
                            "opportunity_type": "clinical AI",
                            "signals": signals,
                            "url": "https://example.org/announcement",
                            "title": "Synthetic announcement",
                        }
                    ]
                }
            )
        )
        return scout_opportunities_fixture(fixture=fixture).records[0]

    canonical = result(["validation study"])
    mixed = result(["validation study", *CAUTIONS])
    assert mixed.source_signals == ["validation study", *CAUTIONS]
    assert _numeric(mixed.score_breakdown) == _numeric(canonical.score_breakdown)
    assert mixed.outside_consulting_likelihood == canonical.outside_consulting_likelihood
    assert (
        mixed.handoff_to_business_research_analyst == canonical.handoff_to_business_research_analyst
    )
    assert "Recognized weighted signals: validation study." in mixed.keystone_fit_reason
