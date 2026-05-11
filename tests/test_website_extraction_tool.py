from __future__ import annotations

from types import SimpleNamespace

import pytest

from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    default_company_page_urls,
    discover_company_page_urls,
    extract_website_content,
    extract_website_content_from_html,
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
