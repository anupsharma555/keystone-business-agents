from __future__ import annotations

from pathlib import Path

from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.schemas.research import ResearchBrief


def test_complete_request_coverage_downgrades_unmet_or_broadened_claims() -> None:
    unmet = RequestCoverage(
        interpreted_request="Return exact official-source matches only.",
        status="complete",
        unmet_dimensions=["official source"],
        output_form_status="satisfied",
        stop_condition_status="satisfied",
    )
    broadened = RequestCoverage(
        interpreted_request="Return zero instead of broadening.",
        status="complete",
        broadened_beyond_request=True,
        output_form_status="not_requested",
        stop_condition_status="satisfied",
    )

    assert unmet.status == "unassessed"
    assert unmet.unmet_dimensions == ["official source"]
    assert broadened.status == "unassessed"
    assert unmet.requires_attention() is True
    assert broadened.requires_attention() is True


def test_complete_request_coverage_normalizes_empty_unmet_sentinels() -> None:
    coverage = RequestCoverage(
        interpreted_request="Return a concise Slack-ready reply.",
        status="complete",
        satisfied_dimensions=["selected thread", "copyable reply"],
        unmet_dimensions=["None", "N/A.", "no unmet dimensions"],
        output_form_status="satisfied",
        stop_condition_status="satisfied",
    )

    assert coverage.unmet_dimensions == []
    assert coverage.requires_attention() is False


def test_partial_or_blocked_coverage_requires_precise_next_action() -> None:
    incomplete = RequestCoverage(
        interpreted_request="Use the selected Gmail thread.",
        status="blocked",
        unmet_dimensions=["selected thread context"],
    )

    coverage = RequestCoverage(
        interpreted_request="Use the selected Gmail thread.",
        status="blocked",
        unmet_dimensions=["selected thread context"],
        output_form_status="unmet",
        stop_condition_status="blocked",
        next_safe_action="Provide or select the exact Gmail thread.",
    )
    assert incomplete.status == "unassessed"
    assert incomplete.unmet_dimensions == ["selected thread context"]
    assert incomplete.requires_attention() is True
    assert coverage.requires_attention() is True


def test_stop_condition_violation_requires_broadening_disclosure() -> None:
    coverage = RequestCoverage(
        interpreted_request="Return zero exact matches if none qualify.",
        status="partial",
        unmet_dimensions=["exact-match stop"],
        stop_condition_status="violated",
        next_safe_action="Remove adjacent matches and return zero.",
    )

    assert coverage.status == "partial"
    assert coverage.broadened_beyond_request is True


def test_overlapping_coverage_dimensions_preserve_the_unmet_claim() -> None:
    coverage = RequestCoverage(
        interpreted_request="Use official sources.",
        status="partial",
        satisfied_dimensions=["official sources", "concise answer"],
        unmet_dimensions=["official sources"],
        next_safe_action="Verify the official source.",
    )

    assert coverage.satisfied_dimensions == ["concise answer"]
    assert coverage.unmet_dimensions == ["official sources"]
    assert coverage.status == "partial"


def test_major_specialist_outputs_share_request_coverage_contract() -> None:
    outputs = (
        EmailTriageResult(
            category="unrelated",
            confidence=1.0,
            reasoning="No selected Gmail context was supplied.",
            recommended_action="Provide a selected thread.",
        ),
        ResearchBrief(target_name="Example"),
        OpportunityScoutResult(),
        OutreachDraft(),
        CompanyProfile(name="Example"),
    )

    assert all(isinstance(output.request_coverage, RequestCoverage) for output in outputs)
    assert all(output.request_coverage.status == "unassessed" for output in outputs)


def test_satisfied_request_coverage_has_no_attention_flag() -> None:
    coverage = RequestCoverage(
        interpreted_request="Return a three-row official-source table.",
        status="complete",
        satisfied_dimensions=["three rows", "official sources", "table"],
        output_form_status="satisfied",
        stop_condition_status="not_requested",
    )

    assert coverage.requires_attention() is False


def test_shared_agent_prompt_requires_request_coverage_without_self_approval() -> None:
    prompt = Path("src/keystone_agents/prompts/agent-operating-architecture.md").read_text(
        encoding="utf-8"
    )

    normalized = " ".join(prompt.split())
    assert "When the output schema includes `request_coverage`" in normalized
    assert "Do not mark coverage complete after broadening" in normalized
    assert "Partial or blocked coverage must name the next safe action" in normalized
