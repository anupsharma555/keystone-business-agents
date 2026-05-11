from __future__ import annotations

from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    RetrievalAutonomyHint,
    RetrievalQualityAssessment,
    assess_opportunity_search_quality,
    build_provider_sequence,
    coerce_retrieval_autonomy_hint,
)
from keystone_agents.schemas.retrieval import RetrievalHint
from keystone_agents.tools.search_provider import SearchProviderError, SearchResult


def _assessment(
    *,
    result_count: int,
    needs_precision_search: bool,
    needs_structured_enrichment: bool = False,
    needs_search_review: bool = False,
    reasons: tuple[str, ...] = (),
) -> RetrievalQualityAssessment:
    return RetrievalQualityAssessment(
        result_count=result_count,
        unique_domain_count=max(1, result_count),
        duplicate_ratio=0.0,
        primary_source_count=max(1, result_count),
        official_source_present=bool(result_count),
        linkedin_source_present=False,
        recent_signal_count=result_count,
        needs_precision_search=needs_precision_search,
        needs_structured_enrichment=needs_structured_enrichment,
        needs_search_review=needs_search_review,
        reasons=reasons,
    )


def test_build_provider_sequence_defaults_to_searxng_then_serper() -> None:
    assert build_provider_sequence(requested_provider=None) == ("searxng", "serper")


def test_build_provider_sequence_respects_configured_primary_override() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="serper",
    ) == ("serper",)


def test_coerce_retrieval_autonomy_hint_accepts_schema_payload() -> None:
    hint = coerce_retrieval_autonomy_hint(
        RetrievalHint(
            source="orchestrator",
            needs_precision_search=True,
            reasons=["strict filters"],
        )
    )

    assert hint is not None
    assert hint.source == "orchestrator"
    assert hint.needs_precision_search is True
    assert hint.reasons == ("strict filters",)


def test_assess_opportunity_search_quality_accepts_multi_lane_packet() -> None:
    assessment = assess_opportunity_search_quality(
        results=[
            SearchResult(
                title="AffectAI SBIR grant award notice",
                link="https://reporter.nih.gov/project-details/123456",
                snippet="NIH SBIR funding supports a behavioral health AI platform.",
                source="serper",
            ),
            SearchResult(
                title="AffectAI platform study",
                link="https://clinicaltrials.gov/study/NCT01234567",
                snippet="Clinical trial launched this week for a psychiatry platform.",
                source="serper",
            ),
            SearchResult(
                title="Precision psychiatry symposium abstract",
                link="https://med.stanford.edu/psychiatry-symposium/abstracts/affectai.html",
                snippet="Conference abstract from Stanford researchers on evidence generation.",
                source="serper",
            ),
        ],
        desired_results=5,
        request_text="Find high-fit live opportunities for Keystone.",
        autonomy_hint=RetrievalAutonomyHint(source="test"),
    )

    assert assessment.needs_precision_search is False
    assert assessment.needs_structured_enrichment is False
    assert {"grant", "trial", "conference"}.issubset(assessment.opportunity_lane_labels)
    assert assessment.opportunity_lane_count >= 3


def test_assess_opportunity_search_quality_flags_company_heavy_packets_for_enrichment() -> None:
    assessment = assess_opportunity_search_quality(
        results=[
            SearchResult(
                title="NeuroAxis raises Series A",
                link="https://news.neuroaxis.com/series-a",
                snippet="Clinical AI startup raises Series A for psychiatry tooling.",
                source="searxng",
            ),
            SearchResult(
                title="NeuroAxis platform",
                link="https://www.neuroaxis.com/platform",
                snippet="Digital mental health platform for evidence generation.",
                source="searxng",
            ),
            SearchResult(
                title="NeuroAxis leadership",
                link="https://www.neuroaxis.com/team",
                snippet="Leadership team for the clinical AI company.",
                source="searxng",
            ),
        ],
        desired_results=5,
        request_text="Find high-fit live opportunities for Keystone.",
        autonomy_hint=RetrievalAutonomyHint(source="test"),
    )

    assert assessment.needs_precision_search is False
    assert assessment.needs_structured_enrichment is True
    assert "company" in assessment.opportunity_lane_labels
    assert any("company-heavy" in reason for reason in assessment.reasons)


def test_hybrid_search_provider_escalates_when_quality_is_weak() -> None:
    calls: list[str] = []

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="Only broad result",
                    link="https://example.test/broad",
                    snippet="Broad result without enough coverage.",
                    source="searxng",
                )
            ]

    class FakeSerperProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"serper:{query}:{num_results}")
            return [
                SearchResult(
                    title="Precise result",
                    link="https://example.test/precise",
                    snippet="Precise result with stronger coverage.",
                    source="serper",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "serper"),
        autonomy_hint=RetrievalAutonomyHint(
            source="test",
            needs_precision_search=True,
            reasons=("strict filters",),
        ),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=len(results) < 2,
            reasons=("too few results",) if len(results) < 2 else (),
        ),
        provider_factory=lambda provider_name: (
            FakeSearxngProvider() if provider_name == "searxng" else FakeSerperProvider()
        ),
    )

    results = provider.search_web("behavioral health ai", num_results=3)
    telemetry = provider.telemetry()

    assert calls == [
        "searxng:behavioral health ai:3",
        "serper:behavioral health ai:3",
    ]
    assert len(results) == 2
    assert telemetry["precision_search_escalated"] is True
    assert telemetry["search_providers_used"] == ["searxng", "serper"]
    assert telemetry["serper_estimated_credits_used"] == 1
    value_summary = {row["provider"]: row for row in telemetry["provider_value_summary"]}
    assert value_summary["serper"]["results_per_success"] == 1.0
    assert value_summary["searxng"]["requests_succeeded"] == 1
    assert provider.collected_results() == results


def test_hybrid_search_provider_can_fan_out_across_providers() -> None:
    calls: list[str] = []

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="Broad result",
                    link="https://example.test/broad",
                    snippet="Broad result.",
                    source="searxng",
                )
            ]

    class FakeSerperProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"serper:{query}:{num_results}")
            return [
                SearchResult(
                    title="Precise result",
                    link="https://example.test/precise",
                    snippet="Precise result.",
                    source="serper",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "serper"),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda provider_name: (
            FakeSearxngProvider() if provider_name == "searxng" else FakeSerperProvider()
        ),
        parallel_provider_fanout=True,
    )

    results = provider.search_web("behavioral health ai", num_results=3)
    telemetry = provider.telemetry()

    assert sorted(calls) == [
        "searxng:behavioral health ai:3",
        "serper:behavioral health ai:3",
    ]
    assert [result.source for result in results] == ["searxng", "serper"]
    assert telemetry["parallel_provider_fanout"] is True
    assert telemetry["search_providers_used"] == ["searxng", "serper"]


def test_hybrid_search_provider_uses_next_provider_after_provider_error() -> None:
    class BrokenPrimaryProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError(f"primary failed for {query}")

    class BackupProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Recovered result",
                    link="https://example.test/recovered",
                    snippet="Recovered from fallback provider.",
                    source="serper",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "serper"),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda provider_name: (
            BrokenPrimaryProvider() if provider_name == "searxng" else BackupProvider()
        ),
    )

    results = provider.search_web("company research", num_results=2)
    telemetry = provider.telemetry()

    assert len(results) == 1
    assert telemetry["provider_error_fallback_used"] is True
    assert telemetry["search_providers_used"] == ["serper"]
    assert telemetry["search_provider_errors"] == [
        {
            "provider": "searxng",
            "error_type": "SearchProviderError",
            "message": "primary failed for company research",
        }
    ]
