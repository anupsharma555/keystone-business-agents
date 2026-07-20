from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionBudget,
    WebsiteExtractionError,
    WebsiteExtractionResult,
    default_company_page_urls,
    discover_company_page_urls,
    extract_website_content,
    extract_website_content_from_html,
    extract_website_content_with_fallbacks,
    website_extraction_page_profile,
    website_extraction_provider_sequence,
)


def test_trafilatura_extracts_claims_from_html() -> None:
    html = """
    <html>
      <head><title>Mentavi About</title></head>
      <body>
        <nav>Menu</nav>
        <main>
          <h1>Mentavi Health</h1>
          <p>Mentavi offers clinician-reviewed mental health diagnostic evaluations for ADHD.</p>
          <p>Mentavi partners with employers and health systems.</p>
        </main>
      </body>
    </html>
    """

    result = extract_website_content_from_html(
        html,
        url="https://mentavi.com/about",
        company_name="Mentavi",
    )

    assert result.provider == "trafilatura"
    assert result.status == "success"
    assert result.title in {"Mentavi About", "Mentavi Health"}
    assert "clinician-reviewed mental health diagnostic evaluations" in result.text_or_markdown
    assert any("employers and health systems" in claim for claim in result.claims)


def test_trafilatura_live_fetch_uses_injected_http_get() -> None:
    def fake_get(*_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            text=(
                "<html><head><title>Curebase</title></head><body>"
                "<main>Curebase provides clinical trial software for research teams.</main>"
                "</body></html>"
            ),
        )

    result = extract_website_content(
        "https://www.curebase.com",
        company_name="Curebase",
        provider="trafilatura",
        live=True,
        http_get=fake_get,
    )

    assert result.provider == "trafilatura"
    assert result.metadata["status_code"] == 200
    assert any("clinical trial software" in claim for claim in result.claims)


def test_firecrawl_extracts_markdown_with_injected_http_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-firecrawl-key")
    monkeypatch.setenv("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev")

    def fake_post(url, **kwargs):
        assert url == "https://api.firecrawl.dev/v2/scrape"
        assert kwargs["headers"]["Authorization"] == "Bearer test-firecrawl-key"
        assert kwargs["json"]["formats"] == ["markdown"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "success": True,
                "data": {
                    "markdown": (
                        "Headway supports mental health providers with insurance workflows."
                    ),
                    "metadata": {"title": "Headway"},
                },
            },
        )

    result = extract_website_content(
        "https://headway.co",
        company_name="Headway",
        provider="firecrawl",
        live=True,
        http_post=fake_post,
    )

    assert result.provider == "firecrawl"
    assert result.title == "Headway"
    assert any("mental health providers" in claim for claim in result.claims)


def test_public_opportunity_extraction_allows_contract_page_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-firecrawl-key")

    def fake_post(*_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "data": {
                    "markdown": (
                        "Contract opportunity for behavioral health AI vendors. "
                        "Login may be required to submit a proposal. Verify now is page copy."
                    ),
                    "metadata": {"title": "Behavioral Health AI RFP"},
                }
            },
        )

    result = extract_website_content(
        "https://sam.gov/opp/example",
        company_name="SAM.gov",
        provider="firecrawl",
        live=True,
        guardrail_context="public_opportunity_source",
        http_post=fake_post,
    )

    assert result.provider == "firecrawl"
    assert "Contract opportunity" in result.text_or_markdown
    assert result.metadata["guardrail_context"] == "public_opportunity_source"


def test_public_web_extraction_allows_incidental_login_page_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-firecrawl-key")

    def fake_post(*_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "data": {
                    "markdown": (
                        "2026 APA Annual Meeting sessions and expert speakers. "
                        "Members may log in to save a personal schedule."
                    ),
                    "metadata": {"title": "2026 APA Annual Meeting"},
                }
            },
        )

    result = extract_website_content(
        "https://www.psychiatry.org/annual-meeting",
        company_name="American Psychiatric Association",
        provider="firecrawl",
        live=True,
        guardrail_context="public_web_source",
        http_post=fake_post,
    )

    assert result.status == "success"
    assert "Members may log in" in result.text_or_markdown
    assert result.metadata["guardrail_context"] == "public_web_source"


def test_firecrawl_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    with pytest.raises(WebsiteExtractionError, match="FIRECRAWL_API_KEY"):
        extract_website_content(
            "https://headway.co",
            company_name="Headway",
            provider="firecrawl",
            live=True,
            http_post=lambda *_args, **_kwargs: None,
        )


def test_crawl4ai_extracts_markdown_with_injected_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeMarkdown:
        fit_markdown = ""
        raw_markdown = "Curebase supports decentralized clinical trial operations."

    class FakeCrawlerResult:
        success = True
        url = "https://www.curebase.com"
        markdown = FakeMarkdown()
        cleaned_html = ""
        html = "<html><title>Curebase</title></html>"
        status_code = 200
        metadata = {"title": "Curebase"}

    class FakeCrawler:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def arun(self, *, url, config):
            assert url == "https://www.curebase.com"
            assert config is not None
            return FakeCrawlerResult()

    fake_module = types.SimpleNamespace(
        AsyncWebCrawler=FakeCrawler,
        BrowserConfig=lambda **kwargs: kwargs,
        CacheMode=types.SimpleNamespace(BYPASS="bypass"),
        CrawlerRunConfig=lambda **kwargs: kwargs,
    )
    monkeypatch.setitem(sys.modules, "crawl4ai", fake_module)

    result = extract_website_content(
        "https://www.curebase.com",
        company_name="Curebase",
        provider="crawl4ai",
        live=True,
    )

    assert result.provider == "crawl4ai"
    assert result.title == "Curebase"
    assert result.metadata["status_code"] == 200
    assert any("decentralized clinical trial operations" in claim for claim in result.claims)


def test_crawl4ai_requires_optional_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "crawl4ai", None)

    with pytest.raises(WebsiteExtractionError, match="Install crawl4ai"):
        extract_website_content(
            "https://www.curebase.com",
            company_name="Curebase",
            provider="crawl4ai",
            live=True,
        )


def test_provider_sequence_defaults_to_local_fallback_without_paid_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", raising=False)

    assert website_extraction_provider_sequence(primary_provider="trafilatura") == (
        "trafilatura",
        "crawl4ai",
    )


def test_provider_sequence_accepts_explicit_ordered_paid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "crawl4ai,firecrawl")

    assert website_extraction_provider_sequence(primary_provider="trafilatura") == (
        "trafilatura",
        "crawl4ai",
        "firecrawl",
    )


def test_js_heavy_page_types_use_local_renderer_first() -> None:
    assert website_extraction_provider_sequence(
        primary_provider="trafilatura",
        fallback_providers=("crawl4ai", "firecrawl"),
        url="https://www.headway.co/careers",
    ) == ("crawl4ai", "trafilatura", "firecrawl")
    assert website_extraction_page_profile("https://sam.gov/search/") == "rendered_first"


def test_ordinary_articles_stay_static_first() -> None:
    assert website_extraction_provider_sequence(
        primary_provider="trafilatura",
        fallback_providers=("crawl4ai", "firecrawl"),
        url="https://www.psychiatry.org/news/article",
    ) == ("trafilatura", "crawl4ai", "firecrawl")


def test_structured_database_page_records_api_preference() -> None:
    assert (
        website_extraction_page_profile(
            "https://clinicaltrials.gov/search?cond=Depression"
        )
        == "structured_api_preferred"
    )


def test_structured_database_skips_general_page_extractors() -> None:
    calls: list[str] = []

    with pytest.raises(WebsiteExtractionError, match="Structured source API is preferred"):
        extract_website_content_with_fallbacks(
            "https://clinicaltrials.gov/search?cond=Depression",
            company_name="ClinicalTrials.gov",
            live=True,
            extractor=lambda *_args, **_kwargs: calls.append("called"),
        )

    assert calls == []


def test_managed_fallback_budget_is_shared_across_selected_pages() -> None:
    calls: list[str] = []
    budget = WebsiteExtractionBudget(firecrawl_max_calls=1)

    def fake_extract(*_args, **kwargs):
        provider = str(kwargs["provider"])
        calls.append(provider)
        if provider != "firecrawl":
            raise WebsiteExtractionError(f"{provider} unavailable")
        return WebsiteExtractionResult(
            url=str(_args[0]),
            provider="firecrawl",
            status="success",
            text_or_markdown="Managed extraction evidence. " * 200,
            claims=["Managed extraction recovered the selected page."],
        )

    first = extract_website_content_with_fallbacks(
        "https://example.com/first",
        company_name="Example",
        fallback_providers=("crawl4ai", "firecrawl"),
        live=True,
        budget=budget,
        extractor=fake_extract,
    )
    with pytest.raises(WebsiteExtractionError, match="managed extraction budget exhausted"):
        extract_website_content_with_fallbacks(
            "https://example.com/second",
            company_name="Example",
            fallback_providers=("crawl4ai", "firecrawl"),
            live=True,
            budget=budget,
            extractor=fake_extract,
        )

    assert first.provider == "firecrawl"
    assert budget.firecrawl_calls_attempted == 1
    assert calls.count("firecrawl") == 1


def test_shared_extraction_ladder_uses_local_renderer_for_shallow_static_result() -> None:
    calls: list[str] = []

    def fake_extract(*_args, **kwargs):
        provider = str(kwargs["provider"])
        calls.append(provider)
        text = "Short company page." if provider == "trafilatura" else "Deep content. " * 300
        return WebsiteExtractionResult(
            url="https://example.com",
            provider=provider,
            status="success",
            text_or_markdown=text,
            claims=["Example provides clinical research services."],
        )

    result = extract_website_content_with_fallbacks(
        "https://example.com",
        company_name="Example",
        primary_provider="trafilatura",
        fallback_providers=("crawl4ai",),
        live=True,
        extractor=fake_extract,
    )

    assert calls == ["trafilatura", "crawl4ai"]
    assert result.provider == "crawl4ai"
    assert result.metadata["fallback_used"] is True
    assert [item["provider"] for item in result.metadata["extraction_attempts"]] == calls


def test_shared_extraction_ladder_preserves_best_result_when_fallback_fails() -> None:
    def fake_extract(*_args, **kwargs):
        provider = str(kwargs["provider"])
        if provider == "crawl4ai":
            raise WebsiteExtractionError("local renderer unavailable")
        return WebsiteExtractionResult(
            url="https://example.com",
            provider=provider,
            status="success",
            text_or_markdown="Useful but compact evidence. " * 20,
            claims=["Example provides clinical research services."],
        )

    result = extract_website_content_with_fallbacks(
        "https://example.com",
        company_name="Example",
        primary_provider="trafilatura",
        fallback_providers=("crawl4ai",),
        live=True,
        extractor=fake_extract,
    )

    assert result.provider == "trafilatura"
    assert result.metadata["quality_gate"] == "no provider met the useful-content threshold"
    assert result.metadata["extraction_attempts"][-1]["status"] == "error"


def test_default_company_page_urls_are_small_and_same_origin() -> None:
    urls = default_company_page_urls("https://mentavi.com/about-mentavi-health/")

    assert urls[0] == "https://mentavi.com"
    assert "https://mentavi.com/about" in urls


def test_discover_company_page_urls_ranks_internal_high_value_links() -> None:
    def fake_get(*_args, **_kwargs):
        return SimpleNamespace(
            status_code=200,
            text=(
                '<a href="/careers">Careers</a>'
                '<a href="/leadership">Leadership</a>'
                '<a href="/partners">Partners</a>'
                '<a href="https://external.example/about">External</a>'
            ),
        )

    urls = discover_company_page_urls(
        "https://mentavi.com",
        company_name="Mentavi",
        live=True,
        http_get=fake_get,
    )

    assert urls[0] == "https://mentavi.com"
    assert "https://mentavi.com/leadership" in urls[:3]
    assert "https://external.example/about" not in urls
    assert len(urls) <= 7
