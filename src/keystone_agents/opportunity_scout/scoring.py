"""Scoring and handoff decisions for Opportunity Scout."""

from __future__ import annotations

from keystone_agents.schemas.opportunity import (
    OpportunityScoreBreakdown,
    OpportunityType,
)
from keystone_agents.source_quality import SourceQualitySummary

TARGET_TYPES: tuple[OpportunityType, ...] = (
    "behavioral health AI",
    "digital mental health",
    "clinical AI",
    "CRO",
    "trial technology",
    "CNS biotech",
    "neurotechnology",
    "grant or collaboration opportunity",
    "journal article or publication call",
    "contract or RFP opportunity",
)

SIGNAL_WEIGHTS: dict[str, int] = {
    "recent funding": 18,
    "hiring clinical": 14,
    "hiring research": 14,
    "hiring AI": 14,
    "hiring product": 10,
    "hiring evidence": 14,
    "payer partnership": 18,
    "product launch": 12,
    "validation study": 18,
    "clinical trial launch": 18,
    "IRB or protocol activity": 16,
    "conference activity": 10,
    "publication or outcomes evidence": 18,
    "journal article call": 14,
    "contract or RFP": 16,
    "partnership announcement": 14,
}

PRIORITY_COMPONENT_WEIGHTS: dict[str, float] = {
    "relevance_score": 0.24,
    "keystone_fit_score": 0.24,
    "source_confidence_score": 0.16,
    "urgency_score": 0.26,
    "next_action_clarity_score": 0.10,
}

TYPE_RELEVANCE_BASE: dict[OpportunityType, int] = {
    "behavioral health AI": 88,
    "digital mental health": 86,
    "clinical AI": 88,
    "CRO": 72,
    "trial technology": 90,
    "CNS biotech": 78,
    "neurotechnology": 82,
    "grant or collaboration opportunity": 58,
    "journal article or publication call": 64,
    "contract or RFP opportunity": 68,
}

TYPE_KEYSTONE_FIT_BASE: dict[OpportunityType, int] = {
    "behavioral health AI": 92,
    "digital mental health": 86,
    "clinical AI": 90,
    "CRO": 72,
    "trial technology": 92,
    "CNS biotech": 76,
    "neurotechnology": 82,
    "grant or collaboration opportunity": 58,
    "journal article or publication call": 68,
    "contract or RFP opportunity": 72,
}

HANDOFF_PRIORITY_THRESHOLD = 70


def normalize_type(value: str) -> OpportunityType:
    """Normalize a free-text opportunity type to the schema literal set."""

    text = value.strip()
    if text in TARGET_TYPES:
        return text  # type: ignore[return-value]
    lowered = text.lower()
    for target in TARGET_TYPES:
        if lowered == target.lower():
            return target
    return "grant or collaboration opportunity"


def bounded_score(value: float) -> int:
    """Bound a score to the 0-100 integer range."""

    return max(0, min(100, round(value)))


def average_score(values: list[int]) -> int:
    """Return a rounded average or zero for an empty list."""

    return round(sum(values) / len(values)) if values else 0


def clean_signals(signals: list[str]) -> list[str]:
    """Normalize and deduplicate signal text."""

    return list(dict.fromkeys(signal.strip() for signal in signals if signal.strip()))


def _has_any_signal(signals: list[str], needles: set[str]) -> bool:
    lowered = {signal.lower() for signal in signals}
    return any(needle.lower() in lowered for needle in needles)


def _relevance_component(signals: list[str], opportunity_type: OpportunityType) -> int:
    base = TYPE_RELEVANCE_BASE[opportunity_type]
    signal_bonus = min(8, len(signals) * 3)
    evidence_bonus = (
        4
        if _has_any_signal(
            signals,
            {
                "validation study",
                "clinical trial launch",
                "publication or outcomes evidence",
                "journal article call",
                "hiring evidence",
            },
        )
        else 0
    )
    return bounded_score(base + signal_bonus + evidence_bonus)


def _keystone_fit_component(signals: list[str], opportunity_type: OpportunityType) -> int:
    base = TYPE_KEYSTONE_FIT_BASE[opportunity_type]
    evidence_bonus = (
        4
        if _has_any_signal(
            signals,
            {
                "validation study",
                "clinical trial launch",
                "publication or outcomes evidence",
                "journal article call",
                "contract or RFP",
                "hiring evidence",
                "IRB or protocol activity",
            },
        )
        else 0
    )
    behavioral_bonus = (
        3 if opportunity_type in {"behavioral health AI", "digital mental health"} else 0
    )
    return bounded_score(base + evidence_bonus + behavioral_bonus)


def _source_confidence_component(summary: SourceQualitySummary | None) -> int:
    return summary.overall_score if summary else 0


def _urgency_component(signals: list[str]) -> int:
    signal_weight = sum(SIGNAL_WEIGHTS.get(signal, 6) for signal in signals)
    return bounded_score(42 + signal_weight * 1.1 + len(signals) * 2)


def _next_action_clarity_component(
    *,
    relevance_score: int,
    keystone_fit_score: int,
    source_confidence_score: int,
    signals: list[str],
    source_count: int,
) -> int:
    score = 45
    score += min(15, len(signals) * 5)
    score += min(12, source_count * 6)
    if relevance_score >= 80 and keystone_fit_score >= 75:
        score += 18
    if source_confidence_score >= 65:
        score += 10
    return bounded_score(score)


def score_from_signals(
    signals: list[str],
    opportunity_type: OpportunityType,
    *,
    source_quality_summary: SourceQualitySummary | None = None,
) -> OpportunityScoreBreakdown:
    """Score an opportunity candidate from normalized signal text and source quality."""

    clean = clean_signals(signals)
    relevance = _relevance_component(clean, opportunity_type)
    keystone_fit = _keystone_fit_component(clean, opportunity_type)
    source_confidence = _source_confidence_component(source_quality_summary)
    urgency = _urgency_component(clean)
    next_action_clarity = _next_action_clarity_component(
        relevance_score=relevance,
        keystone_fit_score=keystone_fit,
        source_confidence_score=source_confidence,
        signals=clean,
        source_count=source_quality_summary.source_count if source_quality_summary else 0,
    )
    priority = bounded_score(
        relevance * PRIORITY_COMPONENT_WEIGHTS["relevance_score"]
        + keystone_fit * PRIORITY_COMPONENT_WEIGHTS["keystone_fit_score"]
        + source_confidence * PRIORITY_COMPONENT_WEIGHTS["source_confidence_score"]
        + urgency * PRIORITY_COMPONENT_WEIGHTS["urgency_score"]
        + next_action_clarity * PRIORITY_COMPONENT_WEIGHTS["next_action_clarity_score"]
    )
    return OpportunityScoreBreakdown(
        relevance_score=relevance,
        keystone_fit_score=keystone_fit,
        source_confidence_score=source_confidence,
        urgency_score=urgency,
        next_action_clarity_score=next_action_clarity,
        priority_score=priority,
        rationale=(
            f"Priority {priority}/100 from relevance {relevance}, Keystone fit "
            f"{keystone_fit}, source confidence {source_confidence}, urgency {urgency}, "
            f"and next-action clarity {next_action_clarity}."
        ),
        component_rationales=[
            f"Relevance {relevance}/100 based on {opportunity_type} and source signals.",
            (
                f"Keystone fit {keystone_fit}/100 based on clinical AI, neuroscience, "
                "behavioral health, evidence, or trial-operations alignment."
            ),
            f"Source confidence {source_confidence}/100 from attributed source quality.",
            f"Urgency {urgency}/100 from why-now signals: {', '.join(clean) or 'none'}.",
            (
                f"Next-action clarity {next_action_clarity}/100 based on evidence and "
                "handoff readiness."
            ),
        ],
    )


def outside_consulting_likelihood(
    breakdown: OpportunityScoreBreakdown,
    *,
    signal_count: int,
) -> int:
    """Estimate outside consulting fit from opportunity score components."""

    return bounded_score(
        average_score(
            [
                breakdown.priority_score,
                breakdown.keystone_fit_score,
                breakdown.urgency_score,
                breakdown.next_action_clarity_score,
            ]
        )
        + min(8, signal_count * 2)
    )


def keystone_fit_reason(
    *,
    opportunity_type: OpportunityType,
    breakdown: OpportunityScoreBreakdown,
    signals: list[str],
) -> str:
    """Explain Keystone fit in source-reviewable language."""

    signal_text = ", ".join(signals) if signals else "no named signals"
    return (
        f"{opportunity_type} opportunity scored {breakdown.keystone_fit_score}/100 "
        "for Keystone fit because the signals touch clinical AI, neuroscience, "
        f"behavioral health, evidence generation, or clinical research operations: {signal_text}."
    )


def handoff_reason(
    *,
    breakdown: OpportunityScoreBreakdown,
    source_quality_summary: SourceQualitySummary | None,
    missing_evidence: list[str] | None = None,
    contradictions: list[str] | None = None,
    weak_evidence_reasons: list[str] | None = None,
) -> str:
    """Explain why Business Research Analyst should or should not receive the candidate."""

    reasons: list[str] = []
    if breakdown.priority_score >= HANDOFF_PRIORITY_THRESHOLD:
        reasons.append(f"priority score {breakdown.priority_score} meets the handoff threshold")
    if source_quality_summary and source_quality_summary.source_count < 2:
        reasons.append("only one attributed source is available")
    if source_quality_summary and source_quality_summary.independent_source_count < 2:
        reasons.append("independent corroboration is still needed")
    if breakdown.source_confidence_score < 70:
        reasons.append("source confidence needs Business Research Analyst validation")
    if contradictions:
        reasons.append("source contradictions require business research review")
    if missing_evidence:
        reasons.append(f"missing evidence: {missing_evidence[0]}")
    if weak_evidence_reasons:
        reasons.append(f"weak evidence: {weak_evidence_reasons[0]}")
    return "; ".join(reasons) if reasons else "Scout queue item does not require handoff yet"


def business_research_analyst_handoff_recommendation(
    *,
    handoff_reason: str,
    research_needed: list[str],
) -> str:
    """Render the Business Research Analyst handoff recommendation."""

    criteria = handoff_reason or "source-backed signal requires validation"
    needs = "; ".join(research_needed[:3]) if research_needed else "validate fit and buyer context"
    return (
        f"Business Research Analyst handoff criteria: {criteria}. Validate {needs} before any "
        "outreach drafting; do not generate outreach copy in Scout."
    )


def should_handoff_to_business_research_analyst(
    *,
    breakdown: OpportunityScoreBreakdown,
    source_quality_summary: SourceQualitySummary | None,
    missing_evidence: list[str],
    contradictions: list[str],
    weak_evidence_reasons: list[str],
) -> bool:
    """Return whether a candidate is ready for Business Research Analyst review."""

    if breakdown.priority_score >= HANDOFF_PRIORITY_THRESHOLD:
        return True
    if contradictions:
        return True
    if breakdown.relevance_score >= 80 and breakdown.keystone_fit_score >= 75:
        return True
    if (
        breakdown.priority_score >= 60
        and (breakdown.relevance_score >= 75 or breakdown.keystone_fit_score >= 75)
        and (missing_evidence or weak_evidence_reasons or breakdown.source_confidence_score < 70)
    ):
        return True
    if (
        source_quality_summary
        and source_quality_summary.source_count < 2
        and (breakdown.relevance_score >= 75 or breakdown.urgency_score >= 70)
    ):
        return True
    return False


def research_needed(
    *,
    source_quality_summary: SourceQualitySummary | None,
    source_count: int,
) -> list[str]:
    """Return standard research follow-ups for a Scout to Business Research Analyst handoff."""

    needs = [
        "Validate company profile, segment, and current business model.",
        "Confirm likely buyer titles and account ownership context.",
    ]
    if source_count < 2:
        needs.append("Find at least one corroborating source for the opportunity signal.")
    if source_quality_summary and source_quality_summary.overall_score < 75:
        needs.append(
            "Improve source confidence with company, academic, government, or news sources."
        )
    return needs
