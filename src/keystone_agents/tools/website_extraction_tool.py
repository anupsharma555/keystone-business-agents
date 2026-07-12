"""Website content extraction tools for company research."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from pydantic import BaseModel, Field

from keystone_agents.config import load_settings, parse_bool
from keystone_agents.guardrails import (
    enforce_public_source_output_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
)
from keystone_agents.source_enrichment import extract_claim_candidates, extract_clean_text

WebsiteExtractorProvider = Literal["trafilatura", "crawl4ai", "firecrawl"]
WebsiteExtractionGuardrailContext = Literal[
    "default",
    "public_opportunity_source",
    "public_web_source",
]


class WebsiteExtractionError(RuntimeError):
    """Raised when a configured website extraction provider fails."""


class WebsiteExtractionResult(BaseModel):
    """Clean extracted page content ready for company research source records."""

    url: str
    title: str = ""
    provider: str
    status: str
    text_or_markdown: str = ""
    claims: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class WebsiteExtractionTool:
    """Explicitly live-gated website extractor."""

    live: bool = False
    provider: WebsiteExtractorProvider = "trafilatura"

    def extract(self, url: str, *, company_name: str) -> WebsiteExtractionResult:
        return extract_website_content(
            url,
            company_name=company_name,
            provider=self.provider,
            live=self.live,
        )


def extract_website_content(
    url: str,
    *,
    company_name: str,
    provider: str | None = None,
    live: bool = False,
    guardrail_context: WebsiteExtractionGuardrailContext = "default",
    http_get: Callable[..., Any] | None = None,
    http_post: Callable[..., Any] | None = None,
) -> WebsiteExtractionResult:
    """Extract readable page text with Trafilatura, Crawl4AI, or Firecrawl."""

    normalized_url = _normalize_url(url)
    resolved_provider = _normalize_provider(provider or load_settings().website_extractor)
    enforce_tool_input_guardrails(
        "website_extract_content",
        {
            "url": normalized_url,
            "company_name": company_name,
            "provider": resolved_provider,
            "live": live,
            "guardrail_context": guardrail_context,
        },
    )
    if not live:
        return _enforce_extraction_output_guardrails(
            WebsiteExtractionResult(
                url=normalized_url,
                provider=resolved_provider,
                status="dry-run",
                metadata={"guardrail_context": guardrail_context},
            ),
            guardrail_context=guardrail_context,
        )
    if resolved_provider == "firecrawl":
        return _extract_with_firecrawl(
            normalized_url,
            company_name=company_name,
            guardrail_context=guardrail_context,
            http_post=http_post,
        )
    if resolved_provider == "crawl4ai":
        return _extract_with_crawl4ai(
            normalized_url,
            company_name=company_name,
            guardrail_context=guardrail_context,
        )
    return _extract_with_trafilatura(
        normalized_url,
        company_name=company_name,
        guardrail_context=guardrail_context,
        http_get=http_get,
    )


def extract_website_content_from_html(
    html: str,
    *,
    url: str,
    company_name: str,
) -> WebsiteExtractionResult:
    """Extract readable text from already-fetched HTML for tests and local adapters."""

    normalized_url = _normalize_url(url)
    text = _trafilatura_extract(html, url=normalized_url) or extract_clean_text(html)
    title = _metadata_title(html)
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    return WebsiteExtractionResult(
        url=normalized_url,
        title=title,
        provider="trafilatura",
        status="success" if text else "empty",
        text_or_markdown=text,
        claims=claims,
        metadata={"title": title} if title else {},
    )


def _extract_with_trafilatura(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
    http_get: Callable[..., Any] | None,
) -> WebsiteExtractionResult:
    get = http_get or requests.get
    try:
        response = get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 KeystoneBusinessAgents/0.1 (company research; contact: operator)"
                )
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise WebsiteExtractionError(f"Website extraction request failed for {url}.") from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise WebsiteExtractionError(
            f"Website extraction failed for {url} with HTTP {status_code}."
        )
    html = str(getattr(response, "text", "") or "")
    result = extract_website_content_from_html(html, url=url, company_name=company_name)
    return _enforce_extraction_output_guardrails(
        result.model_copy(
            update={
                "metadata": {
                    **result.metadata,
                    "status_code": status_code,
                    "guardrail_context": guardrail_context,
                }
            }
        ),
        guardrail_context=guardrail_context,
    )


def _extract_with_firecrawl(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
    http_post: Callable[..., Any] | None,
) -> WebsiteExtractionResult:
    settings = load_settings()
    if not settings.firecrawl_api_key:
        raise WebsiteExtractionError(
            "FIRECRAWL_API_KEY is required when KEYSTONE_WEBSITE_EXTRACTOR=firecrawl."
        )
    post = http_post or requests.post
    endpoint = f"{settings.firecrawl_base_url.rstrip('/')}/v2/scrape"
    try:
        response = post(
            endpoint,
            headers={
                "Authorization": f"Bearer {settings.firecrawl_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "removeBase64Images": True,
                "blockAds": True,
                "timeout": 30000,
            },
            timeout=35,
        )
    except requests.RequestException as exc:
        raise WebsiteExtractionError(f"Firecrawl extraction request failed for {url}.") from exc
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code >= 400:
        raise WebsiteExtractionError(
            f"Firecrawl extraction failed for {url} with HTTP {status_code}."
        )
    data = response.json()
    payload = data.get("data") if isinstance(data, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    text = str(payload.get("markdown") or payload.get("summary") or "").strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    title = str(metadata.get("title") or "").strip()
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    return _enforce_extraction_output_guardrails(
        WebsiteExtractionResult(
            url=url,
            title=title,
            provider="firecrawl",
            status="success" if text else "empty",
            text_or_markdown=text,
            claims=claims,
            metadata={
                **metadata,
                "status_code": status_code,
                "guardrail_context": guardrail_context,
            },
        ),
        guardrail_context=guardrail_context,
    )


def _extract_with_crawl4ai(
    url: str,
    *,
    company_name: str,
    guardrail_context: WebsiteExtractionGuardrailContext,
) -> WebsiteExtractionResult:
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError as exc:
        raise WebsiteExtractionError(
            "Install crawl4ai to use Crawl4AI website extraction."
        ) from exc

    async def _crawl() -> Any:
        browser_config = BrowserConfig(
            browser_type="chromium",
            headless=True,
            text_mode=True,
        )
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            excluded_tags=["script", "style", "nav", "footer"],
            remove_forms=True,
            word_count_threshold=10,
            page_timeout=30000,
        )
        async with AsyncWebCrawler(config=browser_config) as crawler:
            return await crawler.arun(url=url, config=run_config)

    try:
        result = asyncio.run(_crawl())
    except RuntimeError as exc:
        raise WebsiteExtractionError(f"Crawl4AI extraction failed for {url}.") from exc
    except Exception as exc:
        raise WebsiteExtractionError(f"Crawl4AI extraction failed for {url}.") from exc

    success = bool(getattr(result, "success", True))
    if not success:
        message = str(getattr(result, "error_message", "") or "").strip()
        raise WebsiteExtractionError(
            f"Crawl4AI extraction failed for {url}" + (f": {message}" if message else ".")
        )
    markdown = getattr(result, "markdown", None)
    text = str(
        getattr(markdown, "fit_markdown", "")
        or getattr(markdown, "raw_markdown", "")
        or getattr(result, "cleaned_html", "")
        or getattr(result, "html", "")
        or ""
    ).strip()
    if text.startswith("<"):
        text = extract_clean_text(text)
    title = _crawl4ai_title(result)
    claims = extract_claim_candidates(text, company_name=company_name, max_claims=10)
    status_code = int(getattr(result, "status_code", 0) or 0)
    return _enforce_extraction_output_guardrails(
        WebsiteExtractionResult(
            url=str(getattr(result, "url", "") or url),
            title=title,
            provider="crawl4ai",
            status="success" if text else "empty",
            text_or_markdown=text,
            claims=claims,
            metadata={
                **({"status_code": status_code} if status_code else {}),
                **(
                    {"final_url": str(getattr(result, "url", "") or "")}
                    if getattr(result, "url", "")
                    else {}
                ),
                "guardrail_context": guardrail_context,
            },
        ),
        guardrail_context=guardrail_context,
    )


def _enforce_extraction_output_guardrails(
    result: WebsiteExtractionResult,
    *,
    guardrail_context: WebsiteExtractionGuardrailContext,
) -> WebsiteExtractionResult:
    if guardrail_context in {"public_opportunity_source", "public_web_source"}:
        return enforce_public_source_output_guardrails("website_extract_content", result)
    return enforce_tool_output_guardrails("website_extract_content", result)


def _crawl4ai_title(result: Any) -> str:
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
        if title:
            return title
    html = str(getattr(result, "html", "") or getattr(result, "cleaned_html", "") or "")
    return _metadata_title(html) if html else ""


def default_company_page_urls(company_url: str) -> list[str]:
    """Return a small, polite set of likely high-value company pages."""

    origin = _url_origin(_normalize_url(company_url))
    paths = ("", "/about", "/about-us", "/solutions", "/partners", "/newsroom", "/contact")
    return list(dict.fromkeys(urljoin(origin, path) for path in paths))


def discover_company_page_urls(
    company_url: str,
    *,
    company_name: str = "",
    live: bool = False,
    max_urls: int = 6,
    http_get: Callable[..., Any] | None = None,
) -> list[str]:
    """Discover high-value internal company pages from the homepage."""

    origin = _url_origin(_normalize_url(company_url))
    if not live:
        return default_company_page_urls(origin)[:max_urls]
    get = http_get or requests.get
    try:
        response = get(
            origin,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 KeystoneBusinessAgents/0.1 (page discovery; contact: operator)"
                )
            },
            timeout=15,
        )
    except Exception:
        return default_company_page_urls(origin)[:max_urls]
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        return default_company_page_urls(origin)[:max_urls]
    html = str(getattr(response, "text", "") or "")
    candidates = [origin, *_rank_internal_links(origin, html, company_name=company_name)]
    candidates.extend(default_company_page_urls(origin))
    return list(dict.fromkeys(candidates))[:max_urls]


def website_extraction_enabled() -> bool:
    raw = os.getenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION")
    if raw is None:
        return False
    return parse_bool(raw)


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        raise WebsiteExtractionError("A non-empty URL is required for website extraction.")
    if text.startswith("www."):
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WebsiteExtractionError("Website extraction requires an http(s) URL.")
    return text


def _normalize_provider(value: str) -> WebsiteExtractorProvider:
    provider = str(value or "trafilatura").strip().lower()
    if provider in {"local", "trafilatura"}:
        return "trafilatura"
    if provider in {"crawl4ai", "crawl-4-ai", "crawl_4_ai"}:
        return "crawl4ai"
    if provider in {"firecrawl", "firecrawl-cloud"}:
        return "firecrawl"
    raise WebsiteExtractionError(
        "KEYSTONE_WEBSITE_EXTRACTOR must be 'trafilatura', 'crawl4ai', or 'firecrawl'."
    )


def _trafilatura_extract(html: str, *, url: str) -> str:
    try:
        import trafilatura
    except ImportError as exc:  # pragma: no cover - dependency is declared.
        raise WebsiteExtractionError(
            "Install trafilatura to use local website extraction."
        ) from exc
    extracted = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=False,
        include_tables=True,
        favor_recall=True,
    )
    return str(extracted or "").strip()


def _metadata_title(html: str) -> str:
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(html)
    except Exception:
        return ""
    return str(getattr(metadata, "title", "") or "").strip()


def _url_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _rank_internal_links(origin: str, html: str, *, company_name: str) -> list[str]:
    origin_host = urlparse(origin).netloc.lower()
    hrefs = re.findall(r"""href=["']([^"'#]+)["']""", html, flags=re.I)
    scored: list[tuple[int, str]] = []
    company_token = company_name.lower().split()[0] if company_name else ""
    path_weights = {
        "about": 100,
        "team": 95,
        "leadership": 95,
        "contact": 90,
        "partners": 85,
        "solutions": 80,
        "customers": 78,
        "news": 75,
        "press": 75,
        "research": 70,
        "clinical": 70,
        "science": 70,
        "careers": 55,
    }
    for href in hrefs:
        url = urljoin(origin, href.strip())
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != origin_host:
            continue
        lowered = parsed.path.lower()
        score = max(
            (weight for marker, weight in path_weights.items() if marker in lowered),
            default=0,
        )
        if company_token and company_token in url.lower():
            score += 5
        if score:
            scored.append((score, url))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _score, url in scored]
