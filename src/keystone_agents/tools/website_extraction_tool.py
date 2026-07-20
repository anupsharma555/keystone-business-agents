"""Website content extraction tools for company research."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Sequence
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


@dataclass
class WebsiteExtractionBudget:
    """Per-workflow cap for managed extraction calls."""

    firecrawl_max_calls: int = 0
    firecrawl_calls_attempted: int = 0

    def reserve(self, provider: WebsiteExtractorProvider) -> bool:
        if provider != "firecrawl":
            return True
        if self.firecrawl_calls_attempted >= self.firecrawl_max_calls:
            return False
        self.firecrawl_calls_attempted += 1
        return True


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


def extract_website_content_with_fallbacks(
    url: str,
    *,
    company_name: str,
    primary_provider: str | None = None,
    fallback_providers: Sequence[str] | None = None,
    live: bool = False,
    guardrail_context: WebsiteExtractionGuardrailContext = "default",
    minimum_useful_chars: int = 3000,
    budget: WebsiteExtractionBudget | None = None,
    extractor: Callable[..., WebsiteExtractionResult] | None = None,
) -> WebsiteExtractionResult:
    """Extract one page through the shared static, local-rendered, and paid ladder.

    Crawl4AI is the default local fallback after a failed, empty, or shallow
    Trafilatura result. Firecrawl is attempted only when it is explicitly named
    in ``KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK`` or supplied by the caller.
    """

    page_profile = website_extraction_page_profile(url)
    if page_profile == "structured_api_preferred":
        raise WebsiteExtractionError(
            f"Structured source API is preferred over rendered page extraction for {url}."
        )
    extract = extractor or extract_website_content
    providers = website_extraction_provider_sequence(
        primary_provider=primary_provider,
        fallback_providers=fallback_providers,
        url=url,
    )
    active_budget = budget or website_extraction_budget()
    attempts: list[dict[str, Any]] = []
    candidates: list[WebsiteExtractionResult] = []
    errors: list[tuple[str, str]] = []
    for provider in providers:
        if not active_budget.reserve(provider):
            message = "managed extraction budget exhausted"
            attempts.append({"provider": provider, "status": "budget_blocked", "error": message})
            errors.append((provider, message))
            continue
        try:
            result = extract(
                url,
                company_name=company_name,
                provider=provider,
                live=live,
                guardrail_context=guardrail_context,
            )
        except Exception as exc:
            if type(exc).__name__ == "ToolGuardrailViolation":
                message = f"guardrail blocked extraction: {exc}"[:500]
            else:
                message = f"{type(exc).__name__}: {exc}"[:500]
            attempts.append({"provider": provider, "status": "error", "error": message})
            errors.append((provider, message))
            continue
        text_length = len(result.text_or_markdown.strip())
        attempts.append(
            {
                "provider": result.provider,
                "status": result.status,
                "text_length": text_length,
                "claim_count": len(result.claims),
            }
        )
        candidates.append(result)
        if _website_extraction_is_useful(result, minimum_useful_chars=minimum_useful_chars):
            return result.model_copy(
                update={
                    "metadata": {
                        **result.metadata,
                        "extraction_attempts": attempts,
                        "fallback_used": len(attempts) > 1,
                        "extraction_strategy": page_profile,
                    }
                }
            )

    if candidates:
        best = max(candidates, key=_website_extraction_candidate_score)
        return best.model_copy(
            update={
                "metadata": {
                    **best.metadata,
                    "extraction_attempts": attempts,
                    "fallback_used": len(attempts) > 1,
                    "quality_gate": "no provider met the useful-content threshold",
                    "extraction_strategy": page_profile,
                }
            }
        )
    if errors:
        primary_error = f"{errors[0][0]}: {errors[0][1]}"
        fallback_errors = [
            f"fallback {provider}: {_compact_fallback_error(message)}"
            for provider, message in errors[1:]
        ]
        raise WebsiteExtractionError("; ".join([primary_error, *fallback_errors]))
    raise WebsiteExtractionError(f"No website extraction provider returned content for {url}.")


def website_extraction_provider_sequence(
    *,
    primary_provider: str | None = None,
    fallback_providers: Sequence[str] | None = None,
    url: str = "",
) -> tuple[WebsiteExtractorProvider, ...]:
    """Resolve a deduplicated provider ladder without silently enabling paid calls."""

    primary = _normalize_provider(primary_provider or load_settings().website_extractor)
    if fallback_providers is None:
        configured = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "").strip()
        raw_fallbacks = [item for item in configured.split(",") if item.strip()]
        if not raw_fallbacks and primary == "trafilatura":
            raw_fallbacks = ["crawl4ai"]
    else:
        raw_fallbacks = list(fallback_providers)
    normalized = [primary, *(_normalize_provider(item) for item in raw_fallbacks)]
    if website_extraction_page_profile(url) == "rendered_first" and "crawl4ai" in normalized:
        normalized = ["crawl4ai", *(item for item in normalized if item != "crawl4ai")]
    return tuple(dict.fromkeys(normalized))


def website_extraction_page_profile(url: str) -> str:
    """Classify selected URLs for deterministic extraction routing."""

    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "clinicaltrials.gov" and path.startswith("/search"):
        return "structured_api_preferred"
    rendered_hosts = {
        "sam.gov",
        "reporter.nih.gov",
        "boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    }
    rendered_path_markers = (
        "/search",
        "/careers",
        "/jobs",
        "/job/",
        "/funding/searchguide",
    )
    if host in rendered_hosts or any(marker in path for marker in rendered_path_markers):
        return "rendered_first"
    return "static_first"


def website_extraction_budget() -> WebsiteExtractionBudget:
    """Build the explicit per-workflow managed extraction budget."""

    raw = os.getenv("KEYSTONE_FIRECRAWL_EXTRACTION_MAX_CALLS_PER_RUN", "0").strip()
    try:
        maximum = int(raw)
    except ValueError:
        maximum = 0
    return WebsiteExtractionBudget(firecrawl_max_calls=max(0, min(8, maximum)))


def _compact_fallback_error(message: str) -> str:
    if message.startswith("guardrail blocked extraction:"):
        return message
    return message.split(": ", 1)[-1]


def _website_extraction_is_useful(
    result: WebsiteExtractionResult,
    *,
    minimum_useful_chars: int,
) -> bool:
    text = result.text_or_markdown.strip()
    return (
        result.status == "success"
        and bool(result.claims)
        and len(text) >= max(200, int(minimum_useful_chars))
    )


def _website_extraction_candidate_score(result: WebsiteExtractionResult) -> tuple[int, int, int]:
    return (
        int(result.status == "success"),
        len(result.claims),
        len(result.text_or_markdown.strip()),
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
