"""Search provider implementations for dry-run, Serper, SearXNG, and Firecrawl."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urljoin

import requests
from pydantic import BaseModel, Field

from keystone_agents.config import load_settings
from keystone_agents.guardrails import (
    assess_tool_payload_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_NEWS_URL = "https://google.serper.dev/news"
SERPER_IMAGES_URL = "https://google.serper.dev/images"
DEFAULT_TIMEOUT_SECONDS = 10.0


class SearchProviderName(StrEnum):
    """Supported live search provider names."""

    DRY_RUN = "dry-run"
    SERPER = "serper"
    SEARXNG = "searxng"
    FIRECRAWL = "firecrawl"


class SearchProviderConfigurationError(RuntimeError):
    """Raised when a live search provider is missing required configuration."""


class SearchProviderError(RuntimeError):
    """Raised when a live search provider returns an error or malformed response."""


class SerperConfigurationError(SearchProviderConfigurationError):
    """Raised when live Serper search is requested without required configuration."""


class SerperSearchError(SearchProviderError):
    """Raised when Serper returns an error or malformed response."""


class SearxngConfigurationError(SearchProviderConfigurationError):
    """Raised when live SearXNG search is requested without required configuration."""


class SearxngSearchError(SearchProviderError):
    """Raised when SearXNG returns an error or malformed response."""


class FirecrawlConfigurationError(SearchProviderConfigurationError):
    """Raised when live Firecrawl search is requested without required configuration."""


class FirecrawlSearchError(SearchProviderError):
    """Raised when Firecrawl returns an error or malformed response."""


class LiveSearchProviderRequiredError(SearchProviderConfigurationError):
    """Raised when live search is requested while the dry-run provider is selected."""


class SearchRequest(BaseModel):
    """Provider-aware search request.

    The simple string API remains the compatibility path. This structured request lets
    search lanes pass recency, geography, source, and pagination hints without leaking
    provider-specific parameters into agent logic.
    """

    query: str = Field(min_length=1)
    num_results: int = Field(default=5, ge=1)
    source: str = "web"
    categories: list[str] = Field(default_factory=list)
    time_range: str | None = None
    tbs: str | None = None
    country: str | None = None
    location: str | None = None
    language: str | None = None
    page: int | None = Field(default=None, ge=1)
    safe_search: int | None = None
    scrape: bool = False


class SearchResult(BaseModel):
    """Normalized search result with source attribution."""

    title: str = Field(min_length=1)
    link: str = Field(min_length=1)
    snippet: str = ""
    source: str = "search"
    date: str | None = None
    content: str | None = None


def normalize_search_provider_name(value: str | SearchProviderName | None) -> SearchProviderName:
    """Normalize provider configuration into a supported provider name."""

    if value is None or value == "":
        return SearchProviderName.DRY_RUN
    if isinstance(value, SearchProviderName):
        return value
    aliases = {
        "dry_run": SearchProviderName.DRY_RUN,
        "dryrun": SearchProviderName.DRY_RUN,
        "fixture": SearchProviderName.DRY_RUN,
        "fixtures": SearchProviderName.DRY_RUN,
    }
    normalized = str(value).strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return SearchProviderName(normalized)
    except ValueError as exc:
        valid = ", ".join(provider.value for provider in SearchProviderName)
        raise ValueError(f"search provider must be one of: {valid}") from exc


def _validate_query(query: str, num_results: int) -> str:
    resolved_query = query.strip()
    if not resolved_query:
        return ""
    if num_results < 1:
        raise ValueError("num_results must be at least 1.")
    return resolved_query


def _guard_search_input(
    *,
    tool_name: str,
    query: str,
    num_results: int,
    live: bool,
) -> None:
    enforce_tool_input_guardrails(
        tool_name,
        {"query": query, "num_results": num_results, "live": live},
    )


@dataclass(frozen=True)
class DryRunSearchProvider:
    """Search provider that never makes network calls."""

    provider_name: str = "dry-run"
    dry_run: bool = True

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        _guard_search_input(
            tool_name="search_provider_dry_run",
            query=query,
            num_results=num_results,
            live=False,
        )
        _validate_query(query, num_results)
        return enforce_tool_output_guardrails("search_provider_dry_run", [])

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        return self.search_web(request.query, num_results=request.num_results)

    def validate_configuration(self) -> None:
        """Dry-run search needs no credentials or network configuration."""

        return None

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
    ) -> list[SearchResult]:
        result_count = max_results if max_results is not None else num_results
        return self.search_web(query, num_results=result_count)


@dataclass(frozen=True)
class SerperSearchProvider:
    """Serper-backed search provider. It is inert unless `live=True`."""

    live: bool = False
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def dry_run(self) -> bool:
        return not self.live

    def _api_key(self) -> str:
        key = self.api_key or load_settings().serper_api_key
        if not key:
            raise SerperConfigurationError(
                "SERPER_API_KEY is required for live Serper search. "
                "Set it in the environment or .env, or run without --live-search."
            )
        return key

    def validate_configuration(self) -> None:
        """Validate live Serper credentials without making a network call."""

        self._api_key()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        _guard_search_input(
            tool_name="serper_search",
            query=query,
            num_results=num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("serper_search", [])
        results = _search_serper_live(
            request=SearchRequest(query=query, num_results=num_results),
            api_key=self._api_key(),
            timeout_seconds=self.timeout_seconds,
        )
        return _filter_unsafe_search_results("serper_search", results)

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="serper_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("serper_search", [])
        results = _search_serper_live(
            request=request,
            api_key=self._api_key(),
            timeout_seconds=self.timeout_seconds,
        )
        return _filter_unsafe_search_results("serper_search", results)

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
        http_post: Callable[..., Any] | None = None,
    ) -> list[SearchResult] | dict[str, Any]:
        legacy_dict_response = max_results is not None or http_post is not None
        result_count = max_results if max_results is not None else num_results
        _validate_query(query, result_count)
        _guard_search_input(
            tool_name="serper_search",
            query=query,
            num_results=result_count,
            live=self.live,
        )
        if not self.live:
            if legacy_dict_response:
                return enforce_tool_output_guardrails(
                    "serper_search",
                    {"status": "dry-run", "query": query, "results": []},
                )
            return enforce_tool_output_guardrails("serper_search", [])

        results = _search_serper_live(
            request=SearchRequest(query=query, num_results=result_count),
            api_key=self._api_key(),
            timeout_seconds=self.timeout_seconds,
            http_post=http_post,
        )
        results = _filter_unsafe_search_results("serper_search", results)
        if legacy_dict_response:
            return {
                "status": "live",
                "query": query,
                "results": [_result_to_legacy_dict(result) for result in results],
            }
        return results


@dataclass(frozen=True)
class SearxngSearchProvider:
    """SearXNG-backed search provider. It is inert unless `live=True`."""

    live: bool = False
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def dry_run(self) -> bool:
        return not self.live

    def _base_url(self) -> str:
        settings = load_settings()
        base_url = self.base_url or settings.searxng_base_url
        if not base_url:
            raise SearxngConfigurationError(
                "SEARXNG_BASE_URL is required for live SearXNG search. "
                "Set it in the environment or .env, or run without --live-search."
            )
        return base_url.rstrip("/")

    def _api_key(self) -> str | None:
        return self.api_key or load_settings().searxng_api_key

    def validate_configuration(self) -> None:
        """Validate live SearXNG configuration without making a network call."""

        self._base_url()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        _guard_search_input(
            tool_name="searxng_search",
            query=query,
            num_results=num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("searxng_search", [])
        results = _search_searxng_live(
            request=SearchRequest(query=query, num_results=num_results),
            base_url=self._base_url(),
            api_key=self._api_key(),
            timeout_seconds=self.timeout_seconds,
        )
        return _filter_unsafe_search_results("searxng_search", results)

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="searxng_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("searxng_search", [])
        results = _search_searxng_live(
            request=request,
            base_url=self._base_url(),
            api_key=self._api_key(),
            timeout_seconds=self.timeout_seconds,
        )
        return _filter_unsafe_search_results("searxng_search", results)

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
    ) -> list[SearchResult]:
        result_count = max_results if max_results is not None else num_results
        return self.search_web(query, num_results=result_count)


@dataclass(frozen=True)
class FirecrawlSearchProvider:
    """Firecrawl-backed search provider with optional result scraping."""

    live: bool = False
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def dry_run(self) -> bool:
        return not self.live

    def _api_key(self) -> str:
        key = self.api_key or load_settings().firecrawl_api_key
        if not key:
            raise FirecrawlConfigurationError(
                "FIRECRAWL_API_KEY is required for live Firecrawl search. "
                "Set it in the environment or .env, or run without --live-search."
            )
        return key

    def _base_url(self) -> str:
        return (self.base_url or load_settings().firecrawl_base_url).rstrip("/")

    def validate_configuration(self) -> None:
        """Validate live Firecrawl credentials without making a network call."""

        self._api_key()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        return self.search_structured(SearchRequest(query=query, num_results=num_results))

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="firecrawl_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("firecrawl_search", [])
        results = _search_firecrawl_live(
            request=request,
            api_key=self._api_key(),
            base_url=self._base_url(),
            timeout_seconds=self.timeout_seconds,
        )
        return _filter_unsafe_search_results("firecrawl_search", results)

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
    ) -> list[SearchResult]:
        result_count = max_results if max_results is not None else num_results
        return self.search_web(query, num_results=result_count)


def build_search_provider(
    provider: str | SearchProviderName | None = None,
    *,
    live: bool = False,
    api_key: str | None = None,
    searxng_base_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> DryRunSearchProvider | SerperSearchProvider | SearxngSearchProvider | FirecrawlSearchProvider:
    """Build a configured search provider without making a network call."""

    if not live:
        if provider is None:
            return DryRunSearchProvider()
        provider_name = normalize_search_provider_name(provider)
        return DryRunSearchProvider(provider_name=provider_name.value)
    settings = load_settings()
    explicit_provider = provider or settings.search_provider
    if explicit_provider is None or not str(explicit_provider).strip():
        raise LiveSearchProviderRequiredError(
            "Live search requires --search-provider serper or --search-provider searxng "
            "or SEARCH_PROVIDER set to serper or searxng."
        )
    provider_name = normalize_search_provider_name(explicit_provider)
    if provider_name == SearchProviderName.DRY_RUN:
        raise LiveSearchProviderRequiredError(
            "Live search requires --search-provider serper or --search-provider searxng "
            "or SEARCH_PROVIDER set to serper or searxng."
        )
    if provider_name == SearchProviderName.SERPER:
        return SerperSearchProvider(
            live=True,
            api_key=api_key or settings.serper_api_key,
            timeout_seconds=timeout_seconds,
        )
    if provider_name == SearchProviderName.FIRECRAWL:
        return FirecrawlSearchProvider(
            live=True,
            api_key=api_key or settings.firecrawl_api_key,
            base_url=settings.firecrawl_base_url,
            timeout_seconds=timeout_seconds,
        )
    return SearxngSearchProvider(
        live=True,
        base_url=searxng_base_url or settings.searxng_base_url,
        api_key=api_key or settings.searxng_api_key,
        timeout_seconds=timeout_seconds,
    )


def _search_serper_live(
    *,
    request: SearchRequest,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    http_post: Callable[..., Any] | None = None,
) -> list[SearchResult]:
    resolved_query = _validate_query(request.query, request.num_results)
    if not resolved_query:
        return []

    post = http_post or requests.post
    endpoint = _serper_endpoint_for_source(request.source)
    payload: dict[str, Any] = {"q": resolved_query, "num": request.num_results}
    if request.language:
        payload["hl"] = request.language
    if request.country:
        payload["gl"] = request.country.lower()
    if request.location:
        payload["location"] = request.location
    tbs = request.tbs or _google_tbs_for_time_range(request.time_range)
    if tbs:
        payload["tbs"] = tbs
    if request.page is not None:
        payload["page"] = request.page
    try:
        response = post(
            endpoint,
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.Timeout as exc:
        raise SerperSearchError(
            f"Serper search timed out after {timeout_seconds:.1f} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise SerperSearchError("Serper search request failed.") from exc

    data = _response_json(response, provider_name="Serper", error_type=SerperSearchError)
    results: list[SearchResult] = []
    result_key = "news" if _normalized_source(request.source) == "news" else "organic"
    if _normalized_source(request.source) == "images":
        result_key = "images"
    for item in data.get(result_key) or []:
        if not isinstance(item, dict):
            continue
        result = _search_result_from_fields(
            title=item.get("title"),
            link=item.get("link"),
            snippet=item.get("snippet"),
            source="serper",
            date=item.get("date"),
        )
        if result is not None:
            results.append(result)
        if len(results) >= request.num_results:
            break
    return results


def _search_searxng_live(
    *,
    request: SearchRequest,
    base_url: str,
    api_key: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    http_get: Callable[..., Any] | None = None,
) -> list[SearchResult]:
    resolved_query = _validate_query(request.query, request.num_results)
    if not resolved_query:
        return []

    get = http_get or requests.get
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params: dict[str, Any] = {"q": resolved_query, "format": "json"}
    categories = list(request.categories)
    if not categories:
        source = _normalized_source(request.source)
        if source == "news":
            categories = ["news"]
        elif source == "images":
            categories = ["images"]
    if categories:
        params["categories"] = ",".join(categories)
    if request.language:
        params["language"] = request.language
    if request.page is not None:
        params["pageno"] = str(request.page)
    time_range = _searxng_time_range(request.time_range)
    if time_range:
        params["time_range"] = time_range
    if request.safe_search is not None:
        params["safesearch"] = str(request.safe_search)
    try:
        response = get(
            urljoin(f"{base_url.rstrip('/')}/", "search"),
            headers=headers,
            params=params,
            timeout=timeout_seconds,
        )
    except requests.Timeout as exc:
        raise SearxngSearchError(
            f"SearXNG search timed out after {timeout_seconds:.1f} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise SearxngSearchError("SearXNG search request failed.") from exc

    data = _response_json(response, provider_name="SearXNG", error_type=SearxngSearchError)
    results: list[SearchResult] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        result = _search_result_from_fields(
            title=item.get("title"),
            link=item.get("url") or item.get("link"),
            snippet=item.get("content") or item.get("snippet"),
            source="searxng",
            date=item.get("publishedDate") or item.get("date"),
        )
        if result is not None:
            results.append(result)
        if len(results) >= request.num_results:
            break
    return results


def _search_firecrawl_live(
    *,
    request: SearchRequest,
    api_key: str,
    base_url: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    http_post: Callable[..., Any] | None = None,
) -> list[SearchResult]:
    resolved_query = _validate_query(request.query, request.num_results)
    if not resolved_query:
        return []

    post = http_post or requests.post
    source = _normalized_source(request.source)
    payload: dict[str, Any] = {
        "query": resolved_query,
        "limit": request.num_results,
        "sources": [source if source in {"web", "news", "images"} else "web"],
        "timeout": int(timeout_seconds * 1000),
        "ignoreInvalidURLs": True,
    }
    tbs = request.tbs or _google_tbs_for_time_range(request.time_range)
    if tbs:
        payload["tbs"] = tbs
    if request.location:
        payload["location"] = request.location
    if request.country:
        payload["country"] = request.country.upper()
    if request.scrape:
        payload["scrapeOptions"] = {
            "formats": ["markdown"],
            "onlyMainContent": True,
            "removeBase64Images": True,
            "timeout": int(timeout_seconds * 1000),
        }
    try:
        response = post(
            f"{base_url.rstrip('/')}/v2/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.Timeout as exc:
        raise FirecrawlSearchError(
            f"Firecrawl search timed out after {timeout_seconds:.1f} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise FirecrawlSearchError("Firecrawl search request failed.") from exc

    data = _response_json(response, provider_name="Firecrawl", error_type=FirecrawlSearchError)
    payload_data = data.get("data") if isinstance(data, dict) else {}
    if isinstance(payload_data, list):
        items = payload_data
    elif isinstance(payload_data, dict):
        result_key = source if source in {"news", "images"} else "web"
        items = payload_data.get(result_key) or []
    else:
        items = []

    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        markdown = str(item.get("markdown") or "").strip()
        result = _search_result_from_fields(
            title=item.get("title"),
            link=item.get("url"),
            snippet=item.get("description") or item.get("snippet") or markdown[:500],
            source="firecrawl",
            date=item.get("date"),
            content=markdown or item.get("html") or item.get("rawHtml"),
        )
        if result is not None:
            results.append(result)
        if len(results) >= request.num_results:
            break
    return results


def _response_json(
    response: Any,
    *,
    provider_name: str,
    error_type: type[SearchProviderError],
) -> dict[str, Any]:
    status_code = getattr(response, "status_code", None)
    if status_code is not None and status_code >= 400:
        raise error_type(f"{provider_name} search failed with HTTP {status_code}.")
    if status_code is None and hasattr(response, "raise_for_status"):
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise error_type(f"{provider_name} search failed.") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise error_type(f"{provider_name} search returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise error_type(f"{provider_name} search returned invalid JSON.")
    return data


def _search_result_from_fields(
    *,
    title: Any,
    link: Any,
    snippet: Any,
    source: str,
    date: Any = None,
    content: Any = None,
) -> SearchResult | None:
    resolved_title = str(title or "").strip()
    resolved_link = str(link or "").strip()
    if not resolved_title or not resolved_link:
        return None
    return SearchResult(
        title=resolved_title,
        link=resolved_link,
        snippet=str(snippet or "").strip(),
        source=source,
        date=str(date or "").strip() or None,
        content=str(content or "").strip() or None,
    )


def _normalized_source(source: str | None) -> str:
    normalized = str(source or "web").strip().lower()
    if normalized in {"news", "image", "images"}:
        return "images" if normalized in {"image", "images"} else "news"
    return "web"


def _serper_endpoint_for_source(source: str | None) -> str:
    normalized = _normalized_source(source)
    if normalized == "news":
        return SERPER_NEWS_URL
    if normalized == "images":
        return SERPER_IMAGES_URL
    return SERPER_SEARCH_URL


def _google_tbs_for_time_range(time_range: str | None) -> str | None:
    normalized = str(time_range or "").strip().lower()
    mapping = {
        "hour": "qdr:h",
        "day": "qdr:d",
        "week": "qdr:w",
        "recent": "qdr:m",
        "month": "qdr:m",
        "current": "qdr:y",
        "year": "qdr:y",
    }
    return mapping.get(normalized)


def _searxng_time_range(time_range: str | None) -> str | None:
    normalized = str(time_range or "").strip().lower()
    if normalized in {"day", "month", "year"}:
        return normalized
    if normalized in {"week", "recent"}:
        return "month"
    if normalized == "current":
        return "year"
    return None


def _filter_unsafe_search_results(
    tool_name: str,
    results: list[SearchResult],
) -> list[SearchResult]:
    """Drop unsafe individual search hits instead of failing the whole search call."""

    safe_results: list[SearchResult] = []
    for result in results:
        assessment = assess_tool_payload_guardrails(tool_name, [result], output=True)
        if assessment.allowed:
            safe_results.append(result)
    return safe_results


def _result_to_legacy_dict(result: SearchResult) -> dict[str, str]:
    source_type = "metasearch" if result.source == "searxng" else "google_search"
    payload = {
        "title": result.title,
        "link": result.link,
        "url": result.link,
        "snippet": result.snippet,
        "source": result.source,
        "source_type": source_type,
    }
    if result.date:
        payload["date"] = result.date
    if result.content:
        payload["content"] = result.content
    return payload
