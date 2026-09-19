from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.tools.website_extraction_tool import (
    MAX_PUBLIC_URL_CHARS,
    MAX_SELECTED_URLS_PER_BUNDLE,
    WebsiteExtractionBudget,
    WebsiteExtractionError,
    WebsiteExtractionResult,
    build_selected_url_source_bundle,
    default_company_page_urls,
    discover_company_page_urls,
    extract_selected_urls_to_source_bundle,
    extract_website_content,
    extract_website_content_from_html,
    extract_website_content_with_fallbacks,
    validate_public_http_url,
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
    monkeypatch.setenv("KEYSTONE_ENABLE_EXPERIMENTAL_CRAWL4AI", "true")
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
    monkeypatch.setattr(
        "keystone_agents.tools.website_extraction_tool.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ],
    )

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
    monkeypatch.setenv("KEYSTONE_ENABLE_EXPERIMENTAL_CRAWL4AI", "true")
    monkeypatch.setitem(sys.modules, "crawl4ai", None)

    with pytest.raises(WebsiteExtractionError, match="Install crawl4ai"):
        extract_website_content(
            "https://www.curebase.com",
            company_name="Curebase",
            provider="crawl4ai",
            live=True,
        )


def test_provider_sequence_defaults_to_safe_static_provider_without_browser_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", raising=False)

    assert website_extraction_provider_sequence(primary_provider="trafilatura") == (
        "trafilatura",
    )


def test_crawl4ai_live_extraction_is_contained_to_explicit_controlled_evals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_ENABLE_EXPERIMENTAL_CRAWL4AI", raising=False)

    with pytest.raises(WebsiteExtractionError, match="experimental and blocked by default"):
        extract_website_content(
            "https://www.curebase.com",
            company_name="Curebase",
            provider="crawl4ai",
            live=True,
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


def test_selected_url_bundle_defaults_to_dry_run_without_network() -> None:
    calls: list[str] = []

    result = build_selected_url_source_bundle(
        company_name="Example Health",
        selected_urls=[
            "https://example.com/about",
            "https://example.com/about",
            "https://example.com/research",
        ],
        extractor=lambda *_args, **_kwargs: calls.append("called"),
    )

    assert result.mode == "dry_run"
    assert result.selected_url_count == 2
    assert result.extracted_source_count == 0
    assert result.source_bundle.sources == []
    assert [row.selected_url for row in result.diagnostics] == [
        "https://example.com/about",
        "https://example.com/research",
    ]
    assert {row.status for row in result.diagnostics} == {"dry-run"}
    assert calls == []


def test_selected_url_bundle_preserves_source_identity_and_per_url_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")

    def fake_extract(url: str, **kwargs):
        assert kwargs["guardrail_context"] == "public_web_source"
        if url.endswith("/blocked"):
            raise WebsiteExtractionError("provider unavailable")
        return WebsiteExtractionResult(
            url="https://www.example.com/about-us",
            title="Example Health About",
            provider=str(kwargs["provider"]),
            status="success",
            text_or_markdown=(
                "Example Health provides behavioral health research software. " * 90
            ),
            claims=[
                "Example Health provides behavioral health research software."
            ],
            metadata={"final_url": "https://www.example.com/about-us"},
        )

    result = build_selected_url_source_bundle(
        company_name="Example Health",
        company_url="https://example.com",
        selected_urls=[
            "https://example.com/about",
            "https://example.com/blocked",
        ],
        live_extraction=True,
        primary_provider="trafilatura",
        fallback_providers=("crawl4ai",),
        max_text_chars_per_source=500,
        extractor=fake_extract,
    )

    assert result.mode == "live"
    assert result.extracted_source_count == 1
    source = result.source_bundle.sources[0]
    assert source.source_id.startswith("selected-url:")
    assert source.url == "https://example.com/about"
    assert len(source.evidence_excerpt) == 500
    assert source.supported_claims == [
        "Example Health provides behavioral health research software."
    ]
    included, failed = result.diagnostics
    assert included.source_id == source.source_id
    assert included.selected_url == source.url
    assert included.resolved_url == "https://www.example.com/about-us"
    assert included.included_in_bundle is True
    assert included.attempts[0].provider == "trafilatura"
    assert failed.selected_url == "https://example.com/blocked"
    assert failed.status == "error"
    assert [attempt.provider for attempt in failed.attempts] == [
        "trafilatura",
        "crawl4ai",
    ]
    assert "provider unavailable" in failed.error


def test_selected_url_bundle_shares_managed_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    calls: list[str] = []
    budget = WebsiteExtractionBudget(firecrawl_max_calls=1)

    def fake_extract(url: str, **kwargs):
        calls.append(url)
        return WebsiteExtractionResult(
            url=url,
            title="Example",
            provider=str(kwargs["provider"]),
            status="success",
            text_or_markdown="Example provides clinical research software. " * 100,
            claims=["Example provides clinical research software."],
        )

    result = build_selected_url_source_bundle(
        company_name="Example",
        selected_urls=["https://example.com/one", "https://example.com/two"],
        live_extraction=True,
        primary_provider="firecrawl",
        fallback_providers=(),
        budget=budget,
        extractor=fake_extract,
    )

    assert calls == ["https://example.com/one"]
    assert result.firecrawl_max_calls == 1
    assert result.firecrawl_calls_attempted == 1
    assert result.extracted_source_count == 1
    assert result.diagnostics[1].status == "error"
    assert result.diagnostics[1].attempts[0].status == "budget_blocked"
    assert "budget exhausted" in result.diagnostics[1].error


def test_selected_url_bundle_requires_explicit_live_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", raising=False)

    with pytest.raises(WebsiteExtractionError, match="KEYSTONE_ENABLE_WEBSITE_EXTRACTION"):
        build_selected_url_source_bundle(
            company_name="Example",
            selected_urls=["https://example.com"],
            live_extraction=True,
            extractor=lambda *_args, **_kwargs: None,
        )


def test_selected_url_bundle_caps_the_selected_url_batch() -> None:
    with pytest.raises(WebsiteExtractionError, match="capped at"):
        build_selected_url_source_bundle(
            company_name="Example",
            selected_urls=[
                f"https://example.com/page-{index}"
                for index in range(MAX_SELECTED_URLS_PER_BUNDLE + 1)
            ],
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://service.local/admin",
        "http://127.0.0.1/admin",
        "http://10.1.2.3/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0/admin",
        "http://224.0.0.1/admin",
        "http://192.0.2.1/admin",
        "http://[::1]/admin",
        "http://[fc00::1]/admin",
        "http://[fe80::1]/admin",
        "http://[::]/admin",
    ],
)
def test_selected_url_validation_rejects_non_public_literal_targets(url: str) -> None:
    with pytest.raises(WebsiteExtractionError, match="public hostname|blocks loopback"):
        validate_public_http_url(url, resolve_hostname=False)


def test_selected_url_validation_rejects_private_or_mixed_dns_answers() -> None:
    def private_resolver(*_args, **_kwargs):
        return [(2, 1, 6, "", ("10.0.0.8", 443))]

    def mixed_resolver(*_args, **_kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", ("127.0.0.1", 443)),
        ]

    for resolver in (private_resolver, mixed_resolver):
        with pytest.raises(WebsiteExtractionError, match="blocks loopback"):
            validate_public_http_url(
                "https://example.com/research",
                resolve_hostname=True,
                resolver=resolver,
            )


def test_selected_url_validation_accepts_only_public_dns_answers() -> None:
    result = validate_public_http_url(
        "https://example.com/research",
        resolve_hostname=True,
        resolver=lambda *_args, **_kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
        ],
    )

    assert result == "https://example.com/research"


def test_trafilatura_redirect_is_validated_before_following() -> None:
    calls: list[str] = []

    def fake_get(url: str, **_kwargs):
        calls.append(url)
        return SimpleNamespace(
            status_code=302,
            url=url,
            headers={"Location": "http://127.0.0.1/private"},
            text="",
        )

    with pytest.raises(WebsiteExtractionError, match="blocks loopback"):
        extract_website_content(
            "https://example.com/public",
            company_name="Example",
            provider="trafilatura",
            live=True,
            http_get=fake_get,
        )

    assert calls == ["https://example.com/public"]


def test_selected_url_bundle_rejects_unsafe_provider_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")

    def fake_extract(url: str, **kwargs):
        return WebsiteExtractionResult(
            url=url,
            provider=str(kwargs["provider"]),
            status="success",
            text_or_markdown="Example provides clinical research software. " * 100,
            claims=["Example provides clinical research software."],
            metadata={"final_url": "http://169.254.169.254/latest/meta-data"},
        )

    result = build_selected_url_source_bundle(
        company_name="Example",
        selected_urls=["https://example.com/public"],
        live_extraction=True,
        primary_provider="trafilatura",
        fallback_providers=(),
        extractor=fake_extract,
    )

    assert result.extracted_source_count == 0
    assert result.diagnostics[0].status == "error"
    assert "blocks loopback" in result.diagnostics[0].error


def test_selected_url_function_schema_exposes_runtime_bounds() -> None:
    agent = build_business_research_analyst_research_brief_agent()
    schema = next(
        tool.params_json_schema
        for tool in agent.tools
        if tool.name == "extract_selected_urls_to_source_bundle"
    )
    properties = schema["properties"]

    assert properties["company_name"]["maxLength"] == 300
    assert properties["selected_urls"]["minItems"] == 1
    assert properties["selected_urls"]["maxItems"] == MAX_SELECTED_URLS_PER_BUNDLE
    assert properties["selected_urls"]["items"]["maxLength"] == MAX_PUBLIC_URL_CHARS
    assert properties["max_text_chars_per_source"]["minimum"] == 200
    assert properties["max_text_chars_per_source"]["maximum"] == 8000


def test_selected_url_bundle_function_tool_returns_typed_json_shape() -> None:
    result = extract_selected_urls_to_source_bundle(
        company_name="Example",
        selected_urls=["https://example.com/about"],
    )

    assert result["mode"] == "dry_run"
    assert result["source_bundle"]["company_name"] == "Example"
    assert result["diagnostics"][0]["selected_url"] == "https://example.com/about"
    assert getattr(extract_selected_urls_to_source_bundle, "tool_input_guardrails", None)
    assert getattr(extract_selected_urls_to_source_bundle, "tool_output_guardrails", None)


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
