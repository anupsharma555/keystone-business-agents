from __future__ import annotations

from keystone_agents.source_triage import triage_source_candidates
from keystone_agents.tools.search_provider import SearchResult


def test_source_triage_rejects_contradictory_formal_opportunity_source() -> None:
    result = triage_source_candidates(
        agent_name="opportunity_scout",
        request_text=(
            "Find active grant, RFP, or pilot opportunities around youth mental "
            "health AI safety where Keystone could participate."
        ),
        candidates=[
            SearchResult(
                title="School Mental and Behavioral Health",
                link="https://grants.nih.gov/funding/find-a-fit-for-your-research/highlighted-topics/11",
                snippet=(
                    "This is not a notice of funding opportunity (NOFO). Apply "
                    "through an appropriate NIH Parent Funding Announcement."
                ),
                source="exa",
            )
        ],
    )

    assert result.rejected_source_ids == ["source:1"]
    assert result.retained_source_ids == []
    assert result.needs_broaden_or_deepen is True
    assert result.recommended_action == "broaden_or_deepen_before_final_synthesis"
    assert "contradicts" in result.decisions[0].rationale


def test_source_triage_retains_specific_primary_opportunity_source() -> None:
    result = triage_source_candidates(
        agent_name="opportunity_scout",
        request_text=(
            "Find SBIR grant funding opportunities for behavioral health AI "
            "where a small company could apply."
        ),
        candidates=[
            SearchResult(
                title="NIMH SBIR funding opportunity for small businesses",
                link="https://grants.nih.gov/grants/guide/pa-files/PAR-26-001.html",
                snippet=(
                    "NIMH SBIR funding opportunity invites small business grant "
                    "applications for behavioral health technology development."
                ),
                source="searxng",
            )
        ],
    )

    assert result.retained_source_ids == ["source:1"]
    assert result.rejected_source_ids == []
    assert result.needs_broaden_or_deepen is False
    assert result.decisions[0].decision == "retain"


def test_source_triage_rejects_award_recognition_for_formal_opportunity_request() -> None:
    result = triage_source_candidates(
        agent_name="opportunity_scout",
        request_text=(
            "Find active or recently announced pilot, RFP, or grant opportunities "
            "around AI-enabled behavioral health where Keystone could participate."
        ),
        candidates=[
            SearchResult(
                title=(
                    "MedTech Breakthrough Announces 2026 Award Winners: Celebrating "
                    "Digital Health Companies"
                ),
                link="https://example.com/award-winners",
                snippet=(
                    "Awards program recognizes digital health and medical technology "
                    "companies for innovation."
                ),
                source="tavily",
            )
        ],
    )

    assert result.retained_source_ids == []
    assert result.rejected_source_ids == ["source:1"]
    assert result.needs_broaden_or_deepen is True
    assert "recognition" in result.decisions[0].rationale


def test_source_triage_deepens_generic_funding_page_before_retention() -> None:
    result = triage_source_candidates(
        agent_name="opportunity_scout",
        request_text=(
            "Do a deeper source-backed search for grant opportunities around "
            "measurement-based behavioral health AI."
        ),
        candidates=[
            SearchResult(
                title="Behavioral Health Funding Opportunities",
                link="https://example.org/funding-opportunities",
                snippet="Behavioral health funding opportunities and program updates.",
                source="searxng",
            )
        ],
    )

    assert result.retained_source_ids == []
    assert result.deepen_source_ids == ["source:1"]
    assert result.needs_broaden_or_deepen is True
    assert result.decisions[0].decision == "deepen"


def test_source_triage_marks_short_relevant_deep_request_for_deepening() -> None:
    result = triage_source_candidates(
        agent_name="chief_of_staff",
        request_text="Do a deeper source-backed search on OpenAI mental health safety work.",
        candidates=[
            {
                "title": "Update on mental-health-related work",
                "url": "https://openai.com/index/update-on-mental-health-related-work/",
                "snippet": "OpenAI mental health safety work.",
                "source": "agents-web-search",
            }
        ],
    )

    assert result.deepen_source_ids == ["source:1"]
    assert result.needs_broaden_or_deepen is True
    assert result.decisions[0].decision == "deepen"


def test_source_triage_deepens_long_relevant_snippet_without_read_evidence() -> None:
    result = triage_source_candidates(
        agent_name="chief_of_staff",
        request_text=(
            "Can you do a deeper read-only search and detailed synthesis on ambient "
            "AI scribes evaluated in behavioral health clinics?"
        ),
        candidates=[
            {
                "title": "Ambient AI scribe evaluated by behavioral health providers",
                "url": "https://example.org/ambient-ai-scribe-behavioral-health",
                "snippet": (
                    "Ambient AI scribes are being evaluated in behavioral health clinics "
                    "and psychiatry settings for documentation quality, clinician burden, "
                    "patient experience, and workflow fit across outpatient mental health "
                    "teams that review generated notes before signing."
                ),
                "source": "tavily",
                "extraction_status": "snippet_only",
            }
        ],
    )

    assert result.retained_source_ids == []
    assert result.deepen_source_ids == ["source:1"]
    assert result.deepen_urls == ["https://example.org/ambient-ai-scribe-behavioral-health"]
    assert result.needs_broaden_or_deepen is True
    assert any("not yet read/extracted" in reason for reason in result.decisions[0].reasons)


def test_source_triage_retains_extracted_source_for_deep_synthesis() -> None:
    result = triage_source_candidates(
        agent_name="chief_of_staff",
        request_text=(
            "Can you do a deeper read-only search and detailed synthesis on ambient "
            "AI scribes evaluated in behavioral health clinics?"
        ),
        candidates=[
            {
                "title": "AI-Powered Documentation for Mental Health Providers",
                "url": "https://formative.jmir.org/2026/1/e84628",
                "snippet": "",
                "source": "agents-web-search",
                "source_type": "literature",
                "extraction_status": "article_read",
                "evidence_excerpt": (
                    "The article reports a retrospective mixed-methods evaluation of "
                    "AI-powered documentation for mental health providers, including "
                    "workflow use, note review, and provider experience."
                ),
                "supported_claims": [
                    "Mental health providers evaluated AI-powered documentation in practice.",
                    "The study discusses workflow use and provider experience.",
                ],
            }
        ],
    )

    assert result.retained_source_ids == ["source:1"]
    assert result.retained_urls == ["https://formative.jmir.org/2026/1/e84628"]
    assert result.deepen_source_ids == []
    assert result.needs_broaden_or_deepen is False
    assert "extracted/read evidence" in result.decisions[0].rationale
