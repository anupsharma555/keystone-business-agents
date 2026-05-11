from __future__ import annotations

from datetime import date
from pathlib import Path

from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.company_research import research_company_fixture
from keystone_agents.reporting import render_company_profile_report, render_opportunity_scout_report
from keystone_agents.schemas.company_profile import SourceRecord
from keystone_agents.source_enrichment import dedupe_and_rank_source_records
from keystone_agents.source_quality import (
    assess_research_completeness,
    combined_source_confidence,
    score_source_quality,
    summarize_source_quality,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
AS_OF_DATE = date(2026, 4, 21)


def test_company_site_source_scores_high_for_company_facts() -> None:
    score = score_source_quality(
        url="https://www.curebase.com/about",
        title="Curebase company platform",
        declared_source_type="website",
        company_url="https://www.curebase.com",
        supported_text="Curebase is a clinical trial software company.",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    assert score.source_type == "company_site"
    assert score.credibility_score >= 85
    assert score.overall_score >= 75


def test_social_source_scores_lower_for_factual_claims() -> None:
    social = score_source_quality(
        url="https://x.com/example/status/1",
        title="Founder post about product traction",
        declared_source_type="social",
        supported_text="Product traction claim.",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    company_site = score_source_quality(
        url="https://www.example.com/about",
        title="Example company profile",
        declared_source_type="website",
        supported_text="Company profile claim.",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    assert social.source_type == "social"
    assert social.credibility_score < company_site.credibility_score
    assert social.overall_score < company_site.overall_score


def test_stale_source_has_lower_recency() -> None:
    fresh = score_source_quality(
        url="https://reuters.com/example-health-ai",
        title="Health AI partnership",
        declared_source_type="news",
        published_at="2026-01-15",
        as_of_date=AS_OF_DATE,
        claim_context="opportunity_signal",
    )
    stale = score_source_quality(
        url="https://reuters.com/example-health-ai-archive",
        title="Health AI partnership archive",
        declared_source_type="news",
        published_at="2019-01-15",
        as_of_date=AS_OF_DATE,
        claim_context="opportunity_signal",
    )

    assert stale.recency_score < fresh.recency_score
    assert stale.overall_score < fresh.overall_score


def test_unknown_source_has_lower_credibility() -> None:
    unknown = score_source_quality(
        url="https://unknown-example.invalid/post",
        title="Unverified company note",
        supported_text="Unverified factual claim.",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    government = score_source_quality(
        url="https://clinicaltrials.gov/study/NCT00000000",
        title="ClinicalTrials.gov study record",
        supported_text="Study record.",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    assert unknown.source_type == "unknown"
    assert unknown.credibility_score < government.credibility_score
    assert unknown.domain_credibility_score < government.domain_credibility_score


def test_domain_credibility_lifts_reputable_search_result() -> None:
    reputable = score_source_quality(
        url="https://www.reuters.com/business/healthcare-pharmaceuticals/example-validation",
        title="Example announces clinical validation partnership",
        declared_source_type="google_search",
        supported_text="Clinical validation partnership for trial workflow automation.",
        claim_context="opportunity_signal",
        published_at="2026-03-15",
        as_of_date=AS_OF_DATE,
    )
    unknown = score_source_quality(
        url="https://unknown-example.invalid/post",
        title="Example validation note",
        declared_source_type="google_search",
        supported_text="Clinical validation partnership for trial workflow automation.",
        claim_context="opportunity_signal",
        published_at="2026-03-15",
        as_of_date=AS_OF_DATE,
    )

    assert reputable.source_type == "google_search"
    assert reputable.domain_credibility_score > unknown.domain_credibility_score
    assert reputable.overall_score > unknown.overall_score


def test_fixture_and_google_search_sources_are_explicitly_scored() -> None:
    fixture = score_source_quality(
        url="fixture://trial-launch",
        title="Fixture trial launch",
        declared_source_type="fixture",
        supported_text="Clinical trial launch and evidence generation signal.",
        claim_context="opportunity_signal",
        as_of_date=AS_OF_DATE,
    )
    search = score_source_quality(
        url="https://example.test/search-result",
        title="Search result",
        declared_source_type="google_search",
        supported_text="Payer partnership discovery pointer.",
        claim_context="opportunity_signal",
        as_of_date=AS_OF_DATE,
    )

    assert fixture.source_type == "fixture"
    assert search.source_type == "google_search"
    assert fixture.overall_score > search.overall_score


def test_combined_confidence_rewards_corroboration() -> None:
    high = score_source_quality(
        url="https://www.example.com/about",
        title="Example company profile",
        declared_source_type="website",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    second = score_source_quality(
        url="https://clinicaltrials.gov/study/NCT00000000",
        title="ClinicalTrials.gov study record",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    weak = score_source_quality(
        url="https://unknown-example.invalid/post",
        title="Unverified note",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    combined = combined_source_confidence([high, second, weak])
    summary = summarize_source_quality([high, second, weak])

    assert combined > weak.overall_score
    assert combined <= 100
    assert summary.source_count == 3
    assert summary.independent_source_count == 3
    assert summary.overall_score == combined


def test_research_completeness_recommends_stop_for_trusted_combo() -> None:
    company_site = score_source_quality(
        url="https://www.trusttrial.example/about",
        title="TrustTrial clinical trial platform",
        declared_source_type="website",
        company_url="https://www.trusttrial.example",
        supported_text="TrustTrial builds clinical trial software for evidence generation.",
        published_at="2026-03-01",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    news = score_source_quality(
        url="https://www.reuters.com/business/healthcare-pharmaceuticals/trusttrial-validation",
        title="TrustTrial validation partnership",
        declared_source_type="news",
        supported_text="TrustTrial announced a clinical validation partnership.",
        published_at="2026-03-15",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    completeness = assess_research_completeness(
        source_scores=[company_site, news],
        evidence=[
            "TrustTrial builds clinical trial software for evidence generation.",
            "TrustTrial supports decentralized clinical trial operations.",
            "TrustTrial announced a clinical validation partnership.",
        ],
    )

    assert completeness.score >= 82
    assert completeness.stop_recommended is True
    assert "company website evidence" in completeness.satisfied_dimensions
    assert completeness.missing_dimensions == []


def test_duplicate_domain_sources_do_not_overstate_independence() -> None:
    first = score_source_quality(
        url="https://www.example.com/about",
        title="Example company profile",
        declared_source_type="website",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )
    duplicate = score_source_quality(
        url="https://www.example.com/news",
        title="Example company update",
        declared_source_type="website",
        claim_context="company_fact",
        as_of_date=AS_OF_DATE,
    )

    summary = summarize_source_quality([first, duplicate])

    assert summary.source_count == 2
    assert summary.independent_source_count == 1
    assert "1 independent source" in summary.rationale


def test_dedupe_and_rank_source_records_merges_duplicate_urls() -> None:
    ranked = dedupe_and_rank_source_records(
        [
            SourceRecord(
                source_id="company:about",
                title="Example About",
                url="https://www.example.com/about",
                source_type="website",
                supported_claims=["Example builds clinical trial software."],
                confidence=0.7,
            ),
            SourceRecord(
                source_id="company:duplicate",
                title="Example About Copy",
                url="https://example.com/about/",
                source_type="website",
                supported_claims=["Example supports evidence generation."],
                confidence=0.7,
            ),
            SourceRecord(
                source_id="social:1",
                title="Example post",
                url="https://x.com/example/status/1",
                source_type="social",
                supported_claims=["A social post says Example has traction."],
                confidence=0.4,
            ),
        ],
        company_url="https://www.example.com",
    )

    assert len(ranked) == 2
    assert ranked[0].source_id == "company:about"
    assert ranked[0].source_quality is not None
    assert "evidence generation" in " ".join(ranked[0].supported_claims)
    assert ranked[1].source_id == "social:1"


def test_research_and_opportunity_outputs_include_quality_without_live_apis(
    monkeypatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("source quality tests must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_network)

    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )
    scout = scout_opportunities_fixture(max_results=2)

    assert profile.source_quality_summary is not None
    assert profile.source_quality_summary.source_count == len(profile.sources)
    assert all(source.source_quality is not None for source in profile.sources)
    assert scout.source_quality_summary is not None
    assert all(record.source_quality_summary is not None for record in scout.records)
    assert "Source confidence" in render_company_profile_report(profile)
    assert "Source confidence" in render_opportunity_scout_report(scout)
