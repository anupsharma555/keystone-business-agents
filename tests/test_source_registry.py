from __future__ import annotations

from keystone_agents.source_registry import (
    assess_source_coverage,
    classify_source_lanes,
    required_source_lanes_for_company,
    required_source_lanes_for_opportunity,
)
from keystone_agents.tools.search_provider import SearchResult


def test_classify_source_lanes_detects_high_value_domains() -> None:
    assert classify_source_lanes(
        url="https://clinicaltrials.gov/study/NCT01234567",
        title="Clinical trial",
    ) == ("clinical_trials",)
    assert "grants_funding" in classify_source_lanes(
        url="https://reporter.nih.gov/project-details/123",
        title="NIH SBIR grant project",
    )
    assert "careers_jobs" in classify_source_lanes(
        url="https://boards.greenhouse.io/headway/jobs/123",
        title="Clinical strategy role",
    )


def test_classify_source_lanes_recognizes_annual_meeting_pages() -> None:
    lanes = classify_source_lanes(
        url=(
            "https://www.psychiatry.org/psychiatrists/meetings/annual-meeting/"
            "blog/2026-session-search"
        ),
        title="2026 APA Annual Meeting Session Search",
        snippet="Discover expert speakers and sessions for the annual meeting.",
    )

    assert "conference_events" in lanes


def test_assess_source_coverage_reports_missing_lanes_and_domains() -> None:
    coverage = assess_source_coverage(
        [
            SearchResult(
                title="AffectAI validation study - ClinicalTrials.gov",
                link="https://clinicaltrials.gov/study/NCT01234567",
                snippet="Clinical trial recruiting in psychiatry.",
                source="serper",
            ),
            SearchResult(
                title="AffectAI raises seed round",
                link="https://www.businesswire.com/news/affectai-seed",
                snippet="Press release announces funding.",
                source="serper",
            ),
        ],
        expected_lanes=["clinical_trials", "press_news", "grants_funding"],
        expected_domains=["clinicaltrials.gov", "reporter.nih.gov"],
        forbidden_domains=["wikipedia.org"],
    )

    assert coverage.expected_lane_recall == 0.667
    assert coverage.expected_domain_recall == 0.5
    assert coverage.missing_lanes == ("grants_funding",)
    assert coverage.missing_expected_domains == ("reporter.nih.gov",)
    assert coverage.primary_source_count == 1
    assert any("grants_funding" in reason for reason in coverage.diagnosis)


def test_required_source_lanes_are_intent_aware() -> None:
    assert required_source_lanes_for_company(
        company_url="https://example.com",
        request_text="research leadership and clinical validation",
    ) == (
        "company_site",
        "press_news",
        "clinical_trials",
        "literature",
        "people_institutions",
    )
    assert required_source_lanes_for_opportunity(
        request_text="find role, grant, trial, and RFP opportunities",
    ) == (
        "careers_jobs",
        "clinical_trials",
        "grants_funding",
        "procurement_rfp",
    )
