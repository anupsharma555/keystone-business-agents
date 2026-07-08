from __future__ import annotations

from keystone_agents.retrieval_policy import (
    HybridSearchProvider,
    RetrievalAutonomyHint,
    RetrievalQualityAssessment,
    assess_opportunity_search_quality,
    build_provider_sequence,
    build_provider_use_ladder,
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


def test_build_provider_sequence_defaults_to_searxng_only() -> None:
    assert build_provider_sequence(requested_provider=None) == ("searxng",)


def test_build_provider_sequence_respects_configured_primary_override() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="tavily",
    ) == ("tavily",)


def test_build_provider_sequence_requires_serper_enabled_for_configured_primary() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="serper",
    ) == ("searxng",)
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="serper",
        serper_enabled=True,
    ) == ("serper",)


def test_build_provider_sequence_preserves_explicit_requested_serper() -> None:
    assert build_provider_sequence(
        requested_provider="serper",
        configured_provider="searxng",
    ) == ("serper",)


def test_build_provider_sequence_excludes_rendered_providers_from_defaults() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="browserless",
    ) == ("searxng",)
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="searxng",
        fallback_provider="playwright",
    ) == ("searxng",)


def test_build_provider_sequence_allows_explicit_non_serper_fallback() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="searxng",
        fallback_provider="firecrawl",
    ) == ("searxng", "firecrawl")


def test_build_provider_sequence_omits_serper_fallback() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="searxng",
        fallback_provider="serper",
    ) == ("searxng",)


def test_build_provider_sequence_allows_serper_fallback_when_enabled() -> None:
    assert build_provider_sequence(
        requested_provider=None,
        configured_provider="searxng",
        fallback_provider="serper",
        serper_enabled=True,
    ) == ("searxng", "serper")


def test_provider_use_ladder_identifies_default_live_search_lanes() -> None:
    ladder = build_provider_use_ladder(request_text="research Mentavi")

    assert ladder.active_providers() == ("searxng", "agents-web-search")
    assert ladder.rule_for("searxng").use_frequency == "always_default"
    assert ladder.rule_for("agents-web-search").use_frequency == "default_capped"
    assert ladder.rule_for("serper").use_frequency == "disabled"
    assert ladder.rule_for("browserless").use_frequency == "eval_only"


def test_provider_use_ladder_deepens_for_missing_official_lanes() -> None:
    ladder = build_provider_use_ladder(
        request_text="find behavioral health AI grants, RFPs, trials, and conference CFPs",
        missing_source_lanes=("grants_funding", "procurement_rfp", "clinical_trials"),
    )

    assert ladder.rule_for("exa").use_now is True
    assert ladder.rule_for("tavily").use_now is True
    assert ladder.rule_for("exa").stage == "semantic_deepening"
    assert ladder.rule_for("tavily").stage == "research_deepening"


def test_provider_use_ladder_uses_orchestrator_structured_hint_for_exa() -> None:
    ladder = build_provider_use_ladder(
        request_text="research Mentavi",
        autonomy_hint=RetrievalHint(
            source="orchestrator",
            needs_structured_enrichment=True,
            reasons=["leadership context needed"],
        ),
    )

    exa = ladder.rule_for("exa")
    assert exa.use_now is True
    assert "orchestrator_structured_enrichment" in exa.reason_codes
    assert ladder.active_providers() == ("searxng", "agents-web-search", "exa")


def test_provider_use_ladder_uses_orchestrator_precision_for_formal_research() -> None:
    ladder = build_provider_use_ladder(
        request_text="find current behavioral health AI grants and clinical trials",
        autonomy_hint={
            "source": "orchestrator",
            "needs_precision_search": True,
            "reasons": ["strict date and source filters"],
        },
    )

    tavily = ladder.rule_for("tavily")
    assert tavily.use_now is True
    assert "formal_research_request" in tavily.reason_codes
    assert "orchestrator_precision_search" in tavily.reason_codes


def test_provider_use_ladder_keeps_search_review_separate_from_rendered_browser() -> None:
    ladder = build_provider_use_ladder(
        request_text="review conflicting claims for a thin-data company",
        autonomy_hint=RetrievalAutonomyHint(
            source="orchestrator",
            needs_search_review=True,
            reasons=("thin data",),
        ),
    )

    assert ladder.rule_for("playwright").use_now is False
    assert "orchestrator_search_review_not_rendered_browser" in (
        ladder.rule_for("playwright").reason_codes
    )
    assert ladder.rule_for("browserless").use_now is False


def test_provider_use_ladder_separates_extraction_baseline_from_fallbacks() -> None:
    baseline = build_provider_use_ladder(
        request_text="summarize selected source pages",
        requires_extraction=True,
        extraction_quality="strong",
    )
    fallback = build_provider_use_ladder(
        request_text="summarize selected source pages",
        requires_extraction=True,
        extraction_quality="unreadable",
    )

    assert baseline.rule_for("trafilatura").use_now is True
    assert baseline.rule_for("firecrawl").use_now is False
    assert fallback.rule_for("trafilatura").use_now is True
    assert fallback.rule_for("firecrawl").use_now is True
    assert fallback.rule_for("playwright").use_now is True


def test_provider_use_ladder_keeps_rendered_and_future_providers_non_production() -> None:
    ladder = build_provider_use_ladder(
        request_text="inspect rendered page failure",
        rendered_diagnostics_requested=True,
        serper_enabled=True,
    )

    assert ladder.rule_for("playwright").use_frequency == "diagnostic_only"
    assert ladder.rule_for("playwright").use_now is True
    assert ladder.rule_for("browserless").use_now is False
    assert ladder.rule_for("apify").use_frequency == "future_boundary"
    assert ladder.rule_for("crawl4ai").use_frequency == "future_boundary"
    assert ladder.rule_for("serper").use_frequency == "specific_opt_in"
    assert ladder.rule_for("serper").use_now is False


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


def test_assess_opportunity_search_quality_reports_missing_source_lanes() -> None:
    assessment = assess_opportunity_search_quality(
        results=[
            SearchResult(
                title="Behavioral health AI company announces partnership",
                link="https://www.businesswire.com/news/example-partnership",
                snippet="Press release announces a behavioral health AI partnership.",
                source="serper",
            )
        ],
        desired_results=5,
        request_text="Find grant, trial, RFP, and conference opportunities.",
        autonomy_hint=RetrievalAutonomyHint(source="test"),
    )

    assert assessment.needs_precision_search is True
    assert "press_news" in assessment.source_lane_labels
    assert {"clinical_trials", "grants_funding", "procurement_rfp"}.issubset(
        assessment.missing_source_lanes
    )
    assert assessment.source_coverage is not None
    assert any("source coverage missing lanes" in reason for reason in assessment.reasons)


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


def test_hybrid_search_provider_keeps_parallel_optional_errors_nonfatal() -> None:
    class EmptySearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return []

    class FailingAgentsWebSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError("missing hosted web-search credentials")

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "agents-web-search"),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda provider_name: (
            EmptySearxngProvider()
            if provider_name == "searxng"
            else FailingAgentsWebSearchProvider()
        ),
        parallel_provider_fanout=True,
    )

    assert provider.search_web("behavioral health ai", num_results=3) == []
    telemetry = provider.telemetry()

    assert telemetry["search_providers_attempted"] == ["searxng", "agents-web-search"]
    assert telemetry["search_providers_used"] == ["searxng"]
    assert telemetry["search_provider_errors"][0]["provider"] == "agents-web-search"


def test_hybrid_search_provider_degrades_when_all_parallel_providers_timeout() -> None:
    class TimingOutSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise TimeoutError(f"searxng timed out for {query}")

    class TimingOutAgentsWebSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError(f"hosted search timed out for {query}")

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "agents-web-search"),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda provider_name: (
            TimingOutSearxngProvider()
            if provider_name == "searxng"
            else TimingOutAgentsWebSearchProvider()
        ),
        parallel_provider_fanout=True,
    )

    assert provider.search_web("OpenEvidence 2026", num_results=3) == []
    telemetry = provider.telemetry()

    assert telemetry["search_providers_attempted"] == ["searxng", "agents-web-search"]
    assert telemetry["search_providers_used"] == []
    assert sorted(error["error_type"] for error in telemetry["search_provider_errors"]) == [
        "SearchProviderError",
        "TimeoutError",
    ]


def test_hybrid_search_provider_degrades_when_all_sequential_providers_fail() -> None:
    class BrokenPrimaryProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError(f"primary failed for {query}")

    class BrokenBackupProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            raise TimeoutError(f"backup timed out for {query}")

    provider = HybridSearchProvider(
        provider_sequence=("searxng", "serper"),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=True,
        ),
        provider_factory=lambda provider_name: (
            BrokenPrimaryProvider() if provider_name == "searxng" else BrokenBackupProvider()
        ),
    )

    assert provider.search_web("OpenEvidence 2026", num_results=3) == []
    telemetry = provider.telemetry()

    assert telemetry["search_providers_attempted"] == ["searxng", "serper"]
    assert telemetry["search_providers_used"] == []
    assert [error["error_type"] for error in telemetry["search_provider_errors"]] == [
        "SearchProviderError",
        "TimeoutError",
    ]


def test_hybrid_search_provider_runs_deepening_provider_only_after_weak_fast_results() -> None:
    calls: list[str] = []

    class FastProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="Thin company result",
                    link="https://example.test/company",
                    snippet="Company source without careers or press coverage.",
                    source="searxng",
                )
            ]

    class DeepeningProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"agents-web-search:{query}:{num_results}")
            return [
                SearchResult(
                    title="Official careers",
                    link="https://example.test/careers",
                    snippet="Official careers page.",
                    source="agents-web-search",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng",),
        deepening_provider_sequence=("agents-web-search",),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=len(results) < 2,
            reasons=("thin fast results",) if len(results) < 2 else (),
        ),
        provider_factory=lambda provider_name: (
            FastProvider() if provider_name == "searxng" else DeepeningProvider()
        ),
    )

    results = provider.search_web("Mentavi Health", num_results=3)
    telemetry = provider.telemetry()

    assert calls == [
        "searxng:Mentavi Health:3",
        "agents-web-search:Mentavi Health:3",
    ]
    assert [result.source for result in results] == ["searxng", "agents-web-search"]
    assert telemetry["deepening_search_used"] is True
    assert telemetry["search_deepening_provider_sequence"] == ["agents-web-search"]
    assert telemetry["search_providers_used"] == ["searxng", "agents-web-search"]
    assert telemetry["agents_web_search_estimated_calls_used"] == 1


def test_hybrid_search_provider_records_provider_token_cost_usage() -> None:
    class CostedProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Official source",
                    link="https://example.test/source",
                    snippet="Source-backed result.",
                    source="agents-web-search",
                )
            ]

        def last_credit_usage(self) -> dict[str, object]:
            return {
                "request_credits": 1,
                "input_tokens": 1200,
                "cached_input_tokens": 800,
                "output_tokens": 200,
                "reasoning_output_tokens": 50,
                "estimated_usd": 0.0123,
            }

    provider = HybridSearchProvider(
        provider_sequence=("agents-web-search",),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda _provider_name: CostedProvider(),
    )

    provider.search_web("Spring Health 2026", num_results=3)
    usage = provider.telemetry()["provider_usage"]["agents-web-search"]

    assert usage["credits_used"] == 1
    assert usage["input_tokens"] == 1200
    assert usage["cached_input_tokens"] == 800
    assert usage["output_tokens"] == 200
    assert usage["reasoning_output_tokens"] == 50
    assert usage["estimated_usd"] == 0.0123


def test_hybrid_search_provider_deepens_when_structured_enrichment_is_needed() -> None:
    calls: list[str] = []

    class FastProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="Company profile",
                    link="https://example.test/company",
                    snippet="Company source without people or partnership context.",
                    source="searxng",
                )
            ]

    class TavilyDeepeningProvider:
        @property
        def last_credit_usage(self) -> dict[str, object]:
            return {"request_credits": 1, "status": "ok"}

        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"tavily:{query}:{num_results}")
            return [
                SearchResult(
                    title="Company leadership",
                    link="https://example.test/team",
                    snippet="Leadership and partnership context.",
                    source="tavily",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng",),
        deepening_provider_sequence=("tavily",),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
            needs_structured_enrichment=len(results) < 2,
            reasons=("missing structured context",) if len(results) < 2 else (),
        ),
        provider_factory=lambda provider_name: (
            FastProvider() if provider_name == "searxng" else TavilyDeepeningProvider()
        ),
    )

    results = provider.search_web("Mentavi Health leadership", num_results=3)
    telemetry = provider.telemetry()

    assert calls == [
        "searxng:Mentavi Health leadership:3",
        "tavily:Mentavi Health leadership:3",
    ]
    assert [result.source for result in results] == ["searxng", "tavily"]
    assert telemetry["deepening_search_used"] is True
    assert telemetry["tavily_estimated_credits_used"] == 1
    assert telemetry["provider_usage"]["tavily"]["credits_used"] == 1


def test_hybrid_search_provider_skips_deepening_provider_when_fast_results_are_enough() -> None:
    calls: list[str] = []

    class FastProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="Strong result",
                    link="https://example.test/strong",
                    snippet="Strong source coverage.",
                    source="searxng",
                )
            ]

    provider = HybridSearchProvider(
        provider_sequence=("searxng",),
        deepening_provider_sequence=("agents-web-search",),
        autonomy_hint=RetrievalAutonomyHint(source="test"),
        quality_assessor=lambda results, _query: _assessment(
            result_count=len(results),
            needs_precision_search=False,
        ),
        provider_factory=lambda _provider_name: FastProvider(),
    )

    results = provider.search_web("Mentavi Health", num_results=3)
    telemetry = provider.telemetry()

    assert calls == ["searxng:Mentavi Health:3"]
    assert [result.source for result in results] == ["searxng"]
    assert telemetry["deepening_search_used"] is False
    assert telemetry["agents_web_search_estimated_calls_used"] == 0


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
