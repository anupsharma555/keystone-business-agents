from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.tools.search_provider import SearchResult


def test_company_website_extraction_prioritizes_query_focused_company_pages() -> None:
    import keystone_agents.live_retrieval as live_retrieval

    urls = live_retrieval._company_website_extraction_urls(
        company="OpenAI",
        company_url=None,
        queries=[
            "OpenAI 2026 mental health",
            "OpenAI mental health independent coverage 2026",
        ],
        search_results=[
            SearchResult(
                title="OpenAI | Research & Deployment",
                link="https://openai.com/",
                snippet="OpenAI homepage.",
                source="searxng",
            ),
            SearchResult(
                title="About | OpenAI",
                link="https://openai.com/about/",
                snippet="Company mission and structure.",
                source="searxng",
            ),
            SearchResult(
                title="Update on mental-health-related work",
                link="https://openai.com/index/update-on-mental-health-related-work/",
                snippet="OpenAI mental health related work and safety.",
                source="exa",
            ),
            SearchResult(
                title="Introducing Trusted Contact in ChatGPT",
                link="https://openai.com/index/introducing-trusted-contact-in-chatgpt/",
                snippet="Trusted Contact support for sensitive conversations.",
                source="agents-web-search",
            ),
        ],
    )

    assert urls[:2] == [
        "https://openai.com/index/update-on-mental-health-related-work/",
        "https://openai.com/index/introducing-trusted-contact-in-chatgpt/",
    ]
    assert "https://openai.com/" in urls


def test_company_live_retrieval_prioritizes_request_focused_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="OpenAI and Amazon announce strategic partnership",
                    link="https://www.businesswire.com/news/home/openai-amazon-partnership",
                    snippet="OpenAI expands enterprise AI infrastructure with AWS.",
                    source="searxng",
                ),
                SearchResult(
                    title="OpenAI About",
                    link="https://openai.com/about/",
                    snippet="OpenAI is an AI research and deployment company.",
                    source="searxng",
                ),
                SearchResult(
                    title="Update on mental-health-related work",
                    link="https://openai.com/index/update-on-mental-health-related-work/",
                    snippet="OpenAI describes mental health related safety work and sensitive conversations.",
                    source="exa",
                ),
            ]

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "false")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng", website_extractor="trafilatura"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["OpenAI 2026 mental health", "OpenAI mental health trusted contact"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(),
    )

    profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="OpenAI",
        company_url=None,
        request_text=(
            "Can you do a deeper read-only search on what OpenAI is doing "
            "about mental health right now?"
        ),
        max_results=3,
    )

    assert profile.sources[0].url == (
        "https://openai.com/index/update-on-mental-health-related-work/"
    )
    assert metadata["request_focus_terms"][:2] == ["mental", "health"]
    assert metadata["source_focus"]["status"] == "matched_selected_sources"
    assert metadata["source_focus"]["matching_source_count"] >= 1
    assert metadata["retrieval_diagnostics"]["source_focus"]["matching_urls"] == [
        "https://openai.com/index/update-on-mental-health-related-work/"
    ]
    assert metadata["source_triage"]["request_text"].startswith(
        "Can you do a deeper read-only search"
    )
    assert metadata["source_triage"]["deepen_source_ids"] == ["selected:1"]
    assert metadata["source_triage"]["needs_broaden_or_deepen"] is True
    assert metadata["retrieval_diagnostics"]["source_triage"]["deepen_count"] >= 1
    assert metadata["retrieval_diagnostics"]["source_triage"]["decision_counts"]["deepen"] >= 1
    assert metadata["retrieval_diagnostics"]["source_triage"]["deepen_urls"] == [
        "https://openai.com/index/update-on-mental-health-related-work/"
    ]


def test_company_live_retrieval_defaults_to_searxng_with_hosted_parallel_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Curebase funding update",
                    link="https://news.example.test/curebase-funding",
                    snippet="Recent traction signal for Curebase.",
                    source="searxng",
                )
            ]

    class FakeAgentsWebSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Curebase careers",
                    link="https://www.curebase.com/careers",
                    snippet="Official careers page.",
                    source="agents-web-search",
                )
            ]

    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["curebase research"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: {
            "searxng": FakeSearxngProvider(),
            "agents-web-search": FakeAgentsWebSearchProvider(),
        }[provider],
    )
    monkeypatch.setattr(
        live_retrieval,
        "research_account_from_search_results",
        lambda **_kwargs: CompanyProfile(
            name="Curebase",
            website="https://www.curebase.com",
            description="Clinical trial software platform.",
            fit_summary="Relevant for partnership research.",
        ),
    )

    profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="Curebase",
        company_url="https://www.curebase.com",
        max_results=3,
    )

    assert metadata["search_provider"] == "searxng+agents-web-search"
    assert metadata["precision_search_escalated"] is True
    assert metadata["search_provider_sequence"] == ["searxng", "agents-web-search"]
    assert metadata["search_deepening_provider_sequence"] == []
    assert metadata["search_providers_used"] == ["searxng", "agents-web-search"]
    assert metadata["deepening_search_used"] is False
    assert metadata["agents_web_search_estimated_calls_used"] == 1
    assert metadata["serper_estimated_credits_used"] == 0
    assert metadata["structured_enrichment_recommended"] is True
    assert metadata["structured_enrichment_candidates"] == [
        "trafilatura",
        "firecrawl",
        "browserless",
        "apify",
    ]
    assert metadata["search_quality"]["official_source_present"] is True
    diagnostics = metadata["retrieval_diagnostics"]
    assert diagnostics["provider_summary"] == "searxng+agents-web-search"
    assert diagnostics["search_queries"][0] == "curebase research"
    assert diagnostics["hosted_web_search_lane_used"] is True
    assert diagnostics["retrieval_ladder"][0]["raw_result_count"] == 2
    assert diagnostics["search_quality_summary"]["official_source_present"] is True


def test_retrieval_diagnostics_keeps_partial_provider_errors_backend_only() -> None:
    import keystone_agents.live_retrieval as live_retrieval

    diagnostics = live_retrieval.retrieval_diagnostics_from_metadata(
        {
            "mode": "live_search",
            "live_search": True,
            "search_providers_attempted": ["searxng", "agents-web-search"],
            "search_providers_used": ["searxng"],
            "raw_search_result_count": 2,
            "provider_usage": {
                "searxng": {"requests_attempted": 1, "requests_succeeded": 1},
                "agents-web-search": {"requests_attempted": 1, "requests_succeeded": 0},
            },
            "search_provider_errors": [
                {
                    "provider": "agents-web-search",
                    "error_type": "SearchProviderError",
                    "message": "hosted web search timed out",
                }
            ],
        }
    )

    assert diagnostics["errors"] == []


def test_retrieval_diagnostics_keeps_bounded_provider_result_samples() -> None:
    import keystone_agents.live_retrieval as live_retrieval

    diagnostics = live_retrieval.retrieval_diagnostics_from_metadata(
        {
            "mode": "live_search",
            "live_search": True,
            "search_providers_attempted": ["searxng", "exa"],
            "search_providers_used": ["searxng", "exa"],
            "provider_result_samples": {
                "searxng": [
                    {
                        "title": "OpenAI mental health work",
                        "url": "https://openai.com/index/update-on-mental-health-related-work/",
                        "snippet": "OpenAI describes mental health related safety updates.",
                    },
                    {
                        "title": "Extra result",
                        "url": "https://example.com/extra",
                        "snippet": "Extra.",
                    },
                    {
                        "title": "Third result",
                        "url": "https://example.com/third",
                        "snippet": "Third.",
                    },
                    {
                        "title": "Fourth result",
                        "url": "https://example.com/fourth",
                        "snippet": "Should be omitted.",
                    },
                ],
                "exa": [
                    {
                        "title": "Semantic result",
                        "url": "https://example.com/exa",
                        "snippet": "Semantic context.",
                    }
                ],
            },
        }
    )

    samples = diagnostics["provider_result_samples"]
    assert set(samples) == {"searxng", "exa"}
    assert len(samples["searxng"]) == 3
    assert samples["searxng"][0]["url"] == (
        "https://openai.com/index/update-on-mental-health-related-work/"
    )
    assert samples["exa"][0]["snippet"] == "Semantic context."


def test_retrieval_diagnostics_compacts_source_coverage_assessment_shape() -> None:
    import keystone_agents.live_retrieval as live_retrieval

    diagnostics = live_retrieval.retrieval_diagnostics_from_metadata(
        {
            "mode": "live_search",
            "live_search": True,
            "search_provider": "searxng",
            "source_coverage": {
                "observed_lanes": ("company_site", "press_news"),
                "missing_lanes": ("careers_jobs",),
                "lane_counts": {"company_site": 2, "press_news": 1},
                "observed_domains": ("openevidence.com", "techcrunch.com"),
                "primary_source_count": 2,
                "useful_unique_domain_count": 2,
            },
        }
    )

    coverage = diagnostics["source_coverage_summary"]
    assert coverage["selected_source_count"] == 2
    assert coverage["credible_source_count"] == 2
    assert coverage["official_source_count"] == 2
    assert coverage["observed_lane_count"] == 2
    assert coverage["observed_lanes"] == ["company_site", "press_news"]
    assert coverage["missing_lanes"] == ["careers_jobs"]
    assert coverage["observed_domain_count"] == 2


def test_retrieval_diagnostics_surfaces_only_universal_search_failure() -> None:
    import keystone_agents.live_retrieval as live_retrieval

    diagnostics = live_retrieval.retrieval_diagnostics_from_metadata(
        {
            "mode": "live_search",
            "live_search": True,
            "search_providers_attempted": ["searxng", "agents-web-search"],
            "search_providers_used": [],
            "raw_search_result_count": 0,
            "provider_usage": {
                "searxng": {"requests_attempted": 1, "requests_succeeded": 0},
                "agents-web-search": {"requests_attempted": 1, "requests_succeeded": 0},
            },
            "search_provider_errors": [
                {
                    "provider": "searxng",
                    "error_type": "TimeoutError",
                    "message": "timed out",
                },
                {
                    "provider": "agents-web-search",
                    "error_type": "SearchProviderError",
                    "message": "timed out",
                },
            ],
        }
    )

    assert diagnostics["errors"] == [
        "Live search failed across all attempted providers; "
        "backend retrieval telemetry has provider details."
    ]


def test_company_live_retrieval_can_enrich_official_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval
    from keystone_agents.tools.website_extraction_tool import WebsiteExtractionResult

    class FakeProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Mentavi Health",
                    link="https://mentavi.com",
                    snippet="Official mental health diagnostics company site.",
                    source="serper",
                )
            ]

    captured: dict[str, object] = {}

    def fake_profile_builder(**kwargs):
        captured.update(kwargs)
        return CompanyProfile(
            name="Mentavi",
            website="https://mentavi.com",
            description="Mental health diagnostics.",
        )

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "2")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="serper", website_extractor="trafilatura"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["Mentavi official website"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "extract_website_content",
        lambda url, **_kwargs: WebsiteExtractionResult(
            url=url,
            title="Mentavi extracted page",
            provider="trafilatura",
            status="success",
            text_or_markdown="Mentavi offers clinician-reviewed mental health diagnostics.",
            claims=["Mentavi offers clinician-reviewed mental health diagnostics."],
        ),
    )

    profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="Mentavi",
        company_url="https://mentavi.com",
        max_results=2,
        profile_builder=fake_profile_builder,
    )

    website_inputs = captured["website_inputs"]
    assert isinstance(website_inputs, list)
    assert website_inputs
    assert website_inputs[0]["source_type"] == "website"
    assert metadata["website_extraction"]["enabled"] is True
    assert metadata["website_extraction"]["page_count"] == len(website_inputs)


def test_company_live_retrieval_can_agent_review_weak_html_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval
    from keystone_agents.tools.html_review_tool import HtmlReviewResult
    from keystone_agents.tools.website_extraction_tool import WebsiteExtractionResult

    class FakeProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Curebase",
                    link="https://www.curebase.com",
                    snippet="Official site.",
                    source="searxng",
                )
            ]

    captured: dict[str, object] = {}

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setenv("KEYSTONE_AGENT_HTML_REVIEW", "true")
    monkeypatch.setenv("KEYSTONE_AGENT_HTML_REVIEW_MAX_PAGES", "1")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "1")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng", website_extractor="trafilatura"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["Curebase official website"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "extract_website_content",
        lambda url, **_kwargs: WebsiteExtractionResult(
            url=url,
            title="Curebase page",
            provider="trafilatura",
            status="success",
            text_or_markdown="Curebase provides clinical trial software for research teams.",
            claims=[],
        ),
    )
    monkeypatch.setattr(
        live_retrieval,
        "run_agent_html_review",
        lambda **_kwargs: HtmlReviewResult(
            url="https://www.curebase.com",
            title="Curebase page",
            subject="Curebase",
            claims=["Curebase provides clinical trial software for research teams."],
        ),
    )

    _profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="Curebase",
        company_url="https://www.curebase.com",
        max_results=1,
        profile_builder=lambda **kwargs: (
            captured.update(kwargs)
            or CompanyProfile(name="Curebase", website="https://www.curebase.com")
        ),
    )

    website_inputs = captured["website_inputs"]
    assert isinstance(website_inputs, list)
    assert website_inputs[0]["provider"] == "trafilatura+agents-sdk-html-review"
    assert website_inputs[0]["supported_claims"] == [
        "Curebase provides clinical trial software for research teams."
    ]
    assert metadata["website_extraction"]["agent_html_review_page_count"] == 1
    assert metadata["website_extraction"]["agent_html_review_claim_count"] == 1


def test_company_live_retrieval_can_fallback_to_firecrawl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval
    from keystone_agents.tools.website_extraction_tool import (
        WebsiteExtractionError,
        WebsiteExtractionResult,
    )

    class FakeProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Mentavi Health",
                    link="https://mentavi.com",
                    snippet="Official site.",
                    source="serper",
                )
            ]

    def fake_extract(url, *, provider, **_kwargs):
        if provider == "trafilatura":
            raise WebsiteExtractionError("local extraction failed")
        return WebsiteExtractionResult(
            url=url,
            title="Mentavi Firecrawl page",
            provider="firecrawl",
            status="success",
            text_or_markdown="Mentavi works with employers and health systems.",
            claims=["Mentavi works with employers and health systems."],
        )

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "firecrawl")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "1")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="serper", website_extractor="trafilatura"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["Mentavi official website"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(),
    )
    monkeypatch.setattr(live_retrieval, "extract_website_content", fake_extract)

    profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="Mentavi",
        company_url="https://mentavi.com",
        max_results=2,
        profile_builder=lambda **kwargs: CompanyProfile(
            name="Mentavi",
            website="https://mentavi.com",
            description=str(kwargs["website_inputs"][0]["provider"]),
        ),
    )

    assert metadata["website_extraction"]["page_count"] == 1
    assert profile.description == "firecrawl"


def test_company_live_retrieval_skips_failed_website_extraction_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval
    from keystone_agents.tools.website_extraction_tool import WebsiteExtractionError

    class FakeProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="OpenEvidence profile",
                    link="https://sacra.com/c/openevidence/",
                    snippet="Profile page temporarily unavailable.",
                    source="searxng",
                )
            ]

    def fake_extract(url, *, provider, **_kwargs):
        if provider == "trafilatura":
            raise WebsiteExtractionError("Website extraction failed with HTTP 503.")
        raise RuntimeError("fallback provider unavailable")

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "firecrawl")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES", "1")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng", website_extractor="trafilatura"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_company_research_queries",
        lambda *_args: ["OpenEvidence company profile"],
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(),
    )
    monkeypatch.setattr(live_retrieval, "extract_website_content", fake_extract)

    profile, metadata = live_retrieval.retrieve_company_profile_live(
        company="OpenEvidence",
        max_results=1,
        profile_builder=lambda **kwargs: CompanyProfile(
            name="OpenEvidence",
            description=f"website_inputs={len(kwargs['website_inputs'])}",
        ),
    )

    assert profile.description == "website_inputs=0"
    assert metadata["website_extraction"]["page_count"] == 0
    assert (
        "fallback firecrawl: fallback provider unavailable"
        in metadata["website_extraction"]["errors"][0]
    )


def test_opportunity_live_retrieval_fans_out_provider_ladder_for_multi_lane_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    calls: list[str] = []

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"searxng:{query}:{num_results}")
            return [
                SearchResult(
                    title="AffectAI SBIR grant award notice",
                    link="https://reporter.nih.gov/project-details/123456",
                    snippet="NIH grant for a behavioral health AI platform.",
                    source="searxng",
                ),
                SearchResult(
                    title="AffectAI platform study",
                    link="https://clinicaltrials.gov/study/NCT01234567",
                    snippet="Clinical trial launched this week for psychiatry software.",
                    source="searxng",
                ),
                SearchResult(
                    title="Precision psychiatry symposium abstract",
                    link="https://med.stanford.edu/psychiatry-symposium/abstracts/affectai.html",
                    snippet="Conference abstract on clinical AI evidence generation.",
                    source="searxng",
                ),
            ]

    class FakeFirecrawlProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"firecrawl:{query}:{num_results}")
            return [
                SearchResult(
                    title="Parallel provider corroboration",
                    link="https://example.test/fallback",
                    snippet="Parallel provider result is merged into the search packet.",
                    source="firecrawl",
                )
            ]

    class FakeAgentsWebSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"agents-web-search:{query}:{num_results}")
            return []

    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: {
            "searxng": FakeSearxngProvider(),
            "agents-web-search": FakeAgentsWebSearchProvider(),
            "firecrawl": FakeFirecrawlProvider(),
        }[provider],
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="precision psychiatry collaborations",
        fallback_provider="firecrawl",
        desired_results=5,
    )
    results = provider.search_web("precision psychiatry live opportunities", num_results=5)
    telemetry = provider.telemetry()
    quality = live_retrieval.opportunity_search_quality(
        topic="precision psychiatry collaborations",
        max_results=5,
        results=provider.collected_results(),
    )

    assert len(results) == 4
    assert sorted(calls) == [
        "agents-web-search:precision psychiatry live opportunities:5",
        "firecrawl:precision psychiatry live opportunities:5",
        "searxng:precision psychiatry live opportunities:5",
    ]
    assert telemetry["parallel_provider_fanout"] is True
    assert telemetry["precision_search_escalated"] is False
    assert telemetry["search_providers_used"] == ["searxng", "agents-web-search", "firecrawl"]
    assert quality["needs_precision_search"] is False
    assert {"grant", "trial", "conference"}.issubset(quality["opportunity_lane_labels"])


def test_opportunity_role_live_retrieval_defaults_to_searxng_for_job_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="Find remote advisory roles in mental health AI",
        desired_results=5,
    )

    assert provider.provider_sequence == ("searxng", "agents-web-search")
    assert provider.deepening_provider_sequence == ()


def test_opportunity_search_provider_can_disable_agents_web_search_deepening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "false")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="precision psychiatry collaborations",
        desired_results=5,
    )

    assert provider.deepening_provider_sequence == ()


def test_opportunity_search_provider_caps_agents_web_search_parallel_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            calls.append(f"{self.provider_name}:{query}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://{self.provider_name}.example.test/{query.replace(' ', '-')}",
                    snippet="Clinical AI opportunity signal.",
                    source=self.provider_name,
                )
            ]

    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeProvider(str(provider)),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="precision psychiatry collaborations",
        desired_results=3,
    )

    provider.search_web("first query", num_results=3)
    provider.search_web("second query", num_results=3)
    telemetry = provider.telemetry()

    assert calls.count("agents-web-search:first query") == 1
    assert "agents-web-search:second query" not in calls
    assert telemetry["provider_usage"]["agents-web-search"]["requests_attempted"] == 1
    assert any(
        error["error_type"] == "ProviderRequestCapExceeded"
        for error in telemetry["search_provider_errors"]
    )


def test_transient_searxng_runtime_starts_and_stops_when_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(live_retrieval, "_searxng_endpoint_reachable", lambda _base_url: False)
    monkeypatch.setattr(
        live_retrieval,
        "_run_searxng_lifecycle_command",
        lambda command, *, check=True: calls.append((command, check)),
    )

    with live_retrieval._maybe_transient_searxng_runtime(
        provider_sequence=("searxng", "agents-web-search"),
        settings=SimpleNamespace(searxng_base_url="http://127.0.0.1:18080"),
    ) as metadata:
        assert metadata["enabled"] is True
        assert metadata["started"] is True
        assert metadata["reason"] == "started_for_run"

    assert metadata["stopped"] is True
    assert calls == [("start", True), ("stop", False)]


def test_transient_searxng_runtime_leaves_existing_runtime_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    calls: list[str] = []
    monkeypatch.setattr(live_retrieval, "_searxng_endpoint_reachable", lambda _base_url: True)
    monkeypatch.setattr(
        live_retrieval,
        "_run_searxng_lifecycle_command",
        lambda command, *, check=True: calls.append(command),
    )

    with live_retrieval._maybe_transient_searxng_runtime(
        provider_sequence=("searxng",),
        settings=SimpleNamespace(searxng_base_url="http://127.0.0.1:18080"),
    ) as metadata:
        assert metadata["reason"] == "already_running"

    assert metadata["started"] is False
    assert metadata["stopped"] is False
    assert calls == []


def test_opportunity_search_provider_can_enable_tavily_deepening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "true")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="precision psychiatry collaborations",
        desired_results=5,
    )

    assert provider.provider_sequence == ("searxng", "agents-web-search")
    assert provider.deepening_provider_sequence == ("tavily",)


def test_opportunity_search_provider_can_enable_exa_deepening_with_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="precision psychiatry collaborations",
        desired_results=5,
    )

    assert provider.provider_sequence == ("searxng", "agents-web-search")
    assert provider.deepening_provider_sequence == ("exa",)
    assert provider._provider_request_budget.limit_for("exa") == 1


def test_formal_opportunity_search_provider_uses_configured_tavily_deepening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.delenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", raising=False)
    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="searxng", tavily_api_key="test-key"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic=(
            "Find source-backed behavioral-health AI grants, RFPs, pilots, or CFPs "
            "Keystone could act on."
        ),
        desired_results=5,
    )

    assert provider.provider_sequence == ("searxng", "agents-web-search")
    assert provider.deepening_provider_sequence == ("tavily",)


def test_opportunity_search_provider_skips_hosted_search_with_explicit_non_searxng_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    monkeypatch.setattr(
        live_retrieval,
        "load_settings",
        lambda: SimpleNamespace(search_provider="dry-run"),
    )
    monkeypatch.setattr(
        live_retrieval,
        "build_search_provider",
        lambda provider=None, *, live=False: SimpleNamespace(
            provider_name=provider,
            dry_run=False,
            validate_configuration=lambda: None,
            search_web=lambda query, num_results=5: [],
        ),
    )

    provider = live_retrieval.build_opportunity_search_provider(
        topic="identify 5 mental health AI companies",
        requested_provider="serper",
        desired_results=5,
    )

    assert provider.provider_sequence == ("serper",)
    assert provider.deepening_provider_sequence == ()


def test_opportunity_scout_os1_live_search_escalates_on_weak_initial_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Behavioral health AI role",
                    link="https://jobs.example.test/broad-role",
                    snippet="Remote role without recency evidence.",
                    source="searxng",
                )
            ]

    class FakeFirecrawlProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Clinical AI Medical Director",
                    link="https://jobs.example.test/clinical-ai-medical-director",
                    snippet="Remote United States role posted this week.",
                    source="firecrawl",
                )
            ]

    class FakeAgentsWebSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return []

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(search_provider="searxng"))
    monkeypatch.setattr(cli, "_os1_role_search_queries", lambda: ["remote clinical ai"])
    monkeypatch.setattr(
        cli,
        "build_search_provider",
        lambda provider=None, *, live=False: {
            "searxng": FakeSearxngProvider(),
            "agents-web-search": FakeAgentsWebSearchProvider(),
            "firecrawl": FakeFirecrawlProvider(),
        }[provider],
    )
    args = Namespace(
        dry_run=False,
        live_search=True,
        search_provider=None,
        fallback_search_provider="firecrawl",
        max_results=2,
    )

    hits, metadata = cli._search_role_sources_live(args)

    assert len(hits) == 2
    assert metadata["search_provider"] == "searxng+agents-web-search+firecrawl"
    assert metadata["precision_search_escalated"] is True
    assert metadata["search_provider_used"] == "firecrawl"
    assert metadata["search_provider_sequence"] == [
        "searxng",
        "agents-web-search",
        "firecrawl",
    ]
    assert metadata["search_providers_used"] == [
        "searxng",
        "agents-web-search",
        "firecrawl",
    ]


def test_opportunity_scout_explicit_retrieval_hint_drives_search_review_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    class FakeSearxngProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Behavioral health AI role",
                    link="https://jobs.example.test/role-one",
                    snippet="Remote United States role posted this week.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(search_provider="searxng"))
    monkeypatch.setattr(cli, "_os1_role_search_queries", lambda: ["remote clinical ai"])
    monkeypatch.setattr(
        cli,
        "build_search_provider",
        lambda provider=None, *, live=False: FakeSearxngProvider(),
    )
    args = Namespace(
        dry_run=False,
        live_search=True,
        search_provider="searxng",
        fallback_search_provider=None,
        max_results=1,
        retrieval_hint_json=(
            '{"source":"orchestrator","needs_search_review":true,"reasons":["thin data"]}'
        ),
    )

    _hits, metadata = cli._search_role_sources_live(args)

    assert metadata["autonomy_hint"]["source"] == "orchestrator"
    assert metadata["autonomy_hint"]["needs_search_review"] is True
    assert metadata["search_review_recommended"] is True


def test_opportunity_scout_live_retrieval_stages_sandbox_packet_when_review_is_recommended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def telemetry(self) -> dict[str, object]:
            return {
                "search_provider": "searxng",
                "search_review_recommended": True,
                "search_quality": {
                    "needs_search_review": True,
                    "reasons": ["thin corroboration"],
                },
            }

        def collected_results(self) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Thin collaboration signal",
                    link="https://example.test/collaboration",
                    snippet="Possible collaboration mention without corroboration.",
                    source="searxng",
                )
            ]

    monkeypatch.setattr(
        live_retrieval,
        "build_opportunity_search_provider",
        lambda **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "scout_opportunities_live_search",
        lambda **_kwargs: OpportunityScoutResult(
            topic="thin collaboration packet",
            dry_run=False,
            search_provider="searxng",
            search_queries=["thin collaboration packet"],
            records=[],
            audit_notes=[],
        ),
    )

    result, metadata = live_retrieval.run_opportunity_scout_live(
        topic="thin collaboration packet",
        max_results=3,
        sandbox_search_review_artifact_root=tmp_path,
    )

    sandbox_review = metadata["sandbox_search_review"]
    assert sandbox_review["status"] == "staged"
    assert sandbox_review["recommended"] is True
    assert sandbox_review["hosted_web_search"] is True
    packet_file = Path(sandbox_review["packet_file"])
    assert packet_file.exists()
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    assert packet["agent_name"] == "opportunity_scout"
    assert packet["topic"] == "thin collaboration packet"
    assert packet["retrieved_results"][0]["url"] == "https://example.test/collaboration"
    assert any("staged" in note.lower() for note in result.audit_notes)


def test_opportunity_scout_live_retrieval_records_sandbox_hosted_web_search_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def telemetry(self) -> dict[str, object]:
            return {
                "search_provider": "searxng",
                "search_review_recommended": True,
                "search_quality": {
                    "needs_search_review": True,
                    "reasons": ["needs corroboration"],
                },
            }

        def collected_results(self) -> list[SearchResult]:
            return []

    monkeypatch.setattr(
        live_retrieval,
        "build_opportunity_search_provider",
        lambda **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "scout_opportunities_live_search",
        lambda **_kwargs: OpportunityScoutResult(
            topic="sandbox hosted search",
            dry_run=False,
            search_provider="searxng",
            search_queries=["sandbox hosted search"],
            records=[],
            audit_notes=[],
        ),
    )

    result, metadata = live_retrieval.run_opportunity_scout_live(
        topic="sandbox hosted search",
        max_results=3,
        sandbox_search_review_artifact_root=tmp_path,
        sandbox_search_review_hosted_web_search=True,
        sandbox_search_review_hosted_web_search_external_web_access=False,
        sandbox_search_review_hosted_web_search_context_size="high",
    )

    sandbox_review = metadata["sandbox_search_review"]
    assert sandbox_review["hosted_web_search"] is True
    assert sandbox_review["hosted_web_search_external_web_access"] is False
    assert sandbox_review["hosted_web_search_context_size"] == "high"
    assert any("hosted web_search" in note for note in result.audit_notes)


def test_opportunity_scout_live_retrieval_auto_stages_review_when_objective_unmet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def telemetry(self) -> dict[str, object]:
            return {
                "search_provider": "searxng",
                "search_review_recommended": False,
                "search_providers_used": ["searxng"],
                "provider_usage": {},
            }

        def collected_results(self) -> list[SearchResult]:
            return [
                SearchResult(
                    title=f"Candidate {index}",
                    link=f"https://example.test/candidate-{index}",
                    snippet="Weak psychiatry advisory evidence.",
                    source="searxng",
                )
                for index in range(20)
            ]

    monkeypatch.setattr(
        live_retrieval,
        "build_opportunity_search_provider",
        lambda **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "scout_opportunities_live_search",
        lambda **_kwargs: OpportunityScoutResult(
            topic="find one psychiatry AI advisory opportunity",
            dry_run=False,
            search_provider="searxng",
            search_queries=["find one psychiatry AI advisory opportunity"],
            records=[],
            audit_notes=[],
        ),
    )

    _result, metadata = live_retrieval.run_opportunity_scout_live(
        topic="find one psychiatry AI advisory opportunity",
        max_results=1,
        sandbox_search_review_artifact_root=tmp_path,
    )

    sandbox_review = metadata["sandbox_search_review"]
    assert metadata["search_review_recommended"] is True
    assert sandbox_review["hosted_web_search_context_size"] == "low"
    assert sandbox_review["retrieved_result_limit"] == 8
    packet = json.loads(Path(sandbox_review["packet_file"]).read_text(encoding="utf-8"))
    assert packet["retrieved_result_count_total"] == 20
    assert len(packet["retrieved_results"]) == 8
    assert any(
        "opportunity objective was not met" in reason
        for reason in metadata["search_quality"]["reasons"]
    )


def test_opportunity_scout_sandbox_auto_execute_is_env_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def telemetry(self) -> dict[str, object]:
            return {
                "search_provider": "searxng",
                "search_review_recommended": True,
                "search_providers_used": ["searxng"],
                "provider_usage": {},
            }

        def collected_results(self) -> list[SearchResult]:
            return []

    calls: dict[str, object] = {}

    monkeypatch.setenv("KEYSTONE_SANDBOX_SEARCH_REVIEW_AUTO_EXECUTE", "true")
    monkeypatch.setenv("KEYSTONE_SANDBOX_SEARCH_REVIEW_LIVE", "true")
    monkeypatch.setattr(
        live_retrieval,
        "build_opportunity_search_provider",
        lambda **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "scout_opportunities_live_search",
        lambda **_kwargs: OpportunityScoutResult(
            topic="env gated sandbox",
            dry_run=False,
            search_provider="searxng",
            search_queries=["env gated sandbox"],
            records=[],
            audit_notes=[],
        ),
    )
    monkeypatch.setattr(
        live_retrieval,
        "maybe_run_opportunity_scout_sandbox_review",
        lambda **kwargs: calls.update(kwargs) or {"status": "executed"},
    )

    _result, metadata = live_retrieval.run_opportunity_scout_live(
        topic="env gated sandbox",
        max_results=1,
    )

    assert calls["execute"] is True
    assert calls["live"] is True
    assert calls["hosted_web_search_context_size"] == "low"
    assert metadata["sandbox_search_review"]["status"] == "executed"


def test_opportunity_scout_live_retrieval_allows_explicit_disable_of_sandbox_hosted_web_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.live_retrieval as live_retrieval

    class FakeProvider:
        def telemetry(self) -> dict[str, object]:
            return {
                "search_provider": "searxng",
                "search_review_recommended": True,
                "search_quality": {
                    "needs_search_review": True,
                    "reasons": ["needs corroboration"],
                },
            }

        def collected_results(self) -> list[SearchResult]:
            return []

    monkeypatch.setattr(
        live_retrieval,
        "build_opportunity_search_provider",
        lambda **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        live_retrieval,
        "scout_opportunities_live_search",
        lambda **_kwargs: OpportunityScoutResult(
            topic="sandbox hosted search disabled",
            dry_run=False,
            search_provider="searxng",
            search_queries=["sandbox hosted search disabled"],
            records=[],
            audit_notes=[],
        ),
    )

    _result, metadata = live_retrieval.run_opportunity_scout_live(
        topic="sandbox hosted search disabled",
        max_results=3,
        sandbox_search_review_artifact_root=tmp_path,
        sandbox_search_review_hosted_web_search=False,
    )

    sandbox_review = metadata["sandbox_search_review"]
    assert sandbox_review["hosted_web_search"] is False


def test_opportunity_scout_cli_passes_sandbox_review_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import scripts.run_opportunity_scout as cli

    calls: dict[str, object] = {}

    def fake_run_opportunity_scout_live(
        **kwargs: object,
    ) -> tuple[OpportunityScoutResult, dict[str, object]]:
        calls.update(kwargs)
        return (
            OpportunityScoutResult(
                topic="sandbox preview",
                dry_run=False,
                search_provider="searxng",
                search_queries=["sandbox preview"],
                records=[],
                audit_notes=[],
            ),
            {"search_provider": "searxng"},
        )

    monkeypatch.setattr(cli, "run_opportunity_scout_live", fake_run_opportunity_scout_live)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--topic",
            "sandbox preview",
            "--live-search",
            "--no-dry-run",
            "--sandbox-search-review",
            "--force-sandbox-search-review",
            "--sandbox-search-review-web-search",
            "--sandbox-search-review-web-search-context",
            "high",
            "--sandbox-search-review-web-search-cached-only",
            "--sandbox-search-review-artifact-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert cli.main() == 0
    json.loads(capsys.readouterr().out)
    assert calls["sandbox_search_review"] is True
    assert calls["force_sandbox_search_review"] is True
    assert calls["sandbox_search_review_artifact_root"] == str(tmp_path)
    assert calls["sandbox_search_review_hosted_web_search"] is True
    assert calls["sandbox_search_review_hosted_web_search_external_web_access"] is False
    assert calls["sandbox_search_review_hosted_web_search_context_size"] == "high"


def test_opportunity_scout_cli_defaults_sandbox_web_search_when_review_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_opportunity_scout as cli

    calls: dict[str, object] = {}

    def fake_run_opportunity_scout_live(
        **kwargs: object,
    ) -> tuple[OpportunityScoutResult, dict[str, object]]:
        calls.update(kwargs)
        return (
            OpportunityScoutResult(
                topic="sandbox default web search",
                dry_run=False,
                search_provider="searxng",
                search_queries=["sandbox default web search"],
                records=[],
                audit_notes=[],
            ),
            {"search_provider": "searxng"},
        )

    monkeypatch.setattr(cli, "run_opportunity_scout_live", fake_run_opportunity_scout_live)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--topic",
            "sandbox default web search",
            "--live-search",
            "--no-dry-run",
            "--sandbox-search-review",
            "--json",
        ],
    )

    assert cli.main() == 0
    json.loads(capsys.readouterr().out)
    assert calls["sandbox_search_review"] is True
    assert calls["sandbox_search_review_hosted_web_search"] is None
