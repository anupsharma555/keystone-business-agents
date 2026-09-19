"""Search provider implementations for dry-run, Exa, Serper, SearXNG, Firecrawl, and SDK search."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import BaseModel, Field, model_validator

from keystone_agents.config import load_settings
from keystone_agents.guardrails import (
    assess_tool_payload_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
)
from keystone_agents.tavily_usage import (
    record_tavily_credit_usage,
    tavily_credit_preflight,
    tavily_search_credit_estimate,
)

SERPER_SEARCH_URL = "https://google.serper.dev/search"
SERPER_NEWS_URL = "https://google.serper.dev/news"
SERPER_IMAGES_URL = "https://google.serper.dev/images"
EXA_SEARCH_PATH = "/search"
DEFAULT_TIMEOUT_SECONDS = 10.0
_TRACKING_QUERY_PARAMETERS = frozenset(
    {
        "dclid",
        "fbclid",
        "gbraid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "wbraid",
    }
)


class SearchProviderName(StrEnum):
    """Supported live search provider names."""

    DRY_RUN = "dry-run"
    EXA = "exa"
    SERPER = "serper"
    SEARXNG = "searxng"
    FIRECRAWL = "firecrawl"
    TAVILY = "tavily"
    AGENTS_WEB_SEARCH = "agents-web-search"


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


class ExaConfigurationError(SearchProviderConfigurationError):
    """Raised when live Exa search is requested without required configuration."""


class ExaSearchError(SearchProviderError):
    """Raised when Exa returns an error or malformed response."""


class FirecrawlConfigurationError(SearchProviderConfigurationError):
    """Raised when live Firecrawl search is requested without required configuration."""


class FirecrawlSearchError(SearchProviderError):
    """Raised when Firecrawl returns an error or malformed response."""


class TavilyConfigurationError(SearchProviderConfigurationError):
    """Raised when live Tavily search is requested without required configuration."""


class TavilySearchError(SearchProviderError):
    """Raised when Tavily returns an error or malformed response."""


class AgentsWebSearchConfigurationError(SearchProviderConfigurationError):
    """Raised when hosted Agents SDK web search is unavailable or misconfigured."""


class AgentsWebSearchError(SearchProviderError):
    """Raised when hosted Agents SDK web search fails."""


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
    candidate_id: str = Field(default="", max_length=200)
    snippet: str = ""
    source: str = "search"
    date: str | None = None
    content: str | None = None

    @model_validator(mode="after")
    def populate_candidate_id(self) -> SearchResult:
        """Expose one provider-agnostic identity to the model and validators."""

        self.candidate_id = search_result_candidate_id(self.link)
        return self


def canonical_search_result_url(value: object) -> str:
    """Canonicalize a public result URL for cross-provider candidate identity."""

    raw = " ".join(str(value or "").strip().split())
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return raw
    port = parsed.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_PARAMETERS
    ]
    return urlunsplit(
        (scheme, netloc, path, urlencode(sorted(query_pairs), doseq=True), "")
    )


def search_result_candidate_id(value: object) -> str:
    """Return a stable, model-visible identity derived from canonical URL."""

    canonical_url = canonical_search_result_url(value)
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:24]
    return f"web-candidate:{digest}"


class AgentsWebSearchResult(BaseModel):
    """One source-attributed result returned by hosted Agents SDK web search."""

    title: str = ""
    url: str = ""
    snippet: str = ""
    date: str | None = None


class AgentsWebSearchOutput(BaseModel):
    """Structured output expected from the hosted Agents SDK web-search adapter."""

    results: list[AgentsWebSearchResult] = Field(default_factory=list)


@dataclass(frozen=True)
class TavilySearchPacket:
    """Internal Tavily response packet with normalized results and credit usage."""

    results: list[SearchResult]
    credits_used: int
    usage_source: str


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
        "tavily-search": SearchProviderName.TAVILY,
        "openai-web-search": SearchProviderName.AGENTS_WEB_SEARCH,
        "openai-websearch": SearchProviderName.AGENTS_WEB_SEARCH,
        "agents-websearch": SearchProviderName.AGENTS_WEB_SEARCH,
        "sdk-web-search": SearchProviderName.AGENTS_WEB_SEARCH,
        "native-web-search": SearchProviderName.AGENTS_WEB_SEARCH,
    }
    normalized = str(value).strip().lower().replace("_", "-")
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
class ExaSearchProvider:
    """Exa-backed AI search provider with optional bounded page contents."""

    live: bool = False
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    _last_credit_usage: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def dry_run(self) -> bool:
        return not self.live

    def _settings(self) -> Any:
        return load_settings()

    def _api_key(self) -> str:
        key = self.api_key or self._settings().exa_api_key
        if not key:
            raise ExaConfigurationError(
                "EXA_API_KEY is required for live Exa search. "
                "Set it in the environment or .env, or run without --live-search."
            )
        return key

    def _base_url(self) -> str:
        return (self.base_url or self._settings().exa_base_url).rstrip("/")

    def validate_configuration(self) -> None:
        """Validate live Exa credentials without making a network call."""

        self._api_key()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        return self.search_structured(SearchRequest(query=query, num_results=num_results))

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="exa_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("exa_search", [])
        results = _search_exa_live(
            request=request,
            api_key=self._api_key(),
            base_url=self._base_url(),
            timeout_seconds=self.timeout_seconds,
        )
        object.__setattr__(
            self,
            "_last_credit_usage",
            {"request_credits": 1, "usage_source": "provider_request_estimate"},
        )
        return _filter_unsafe_search_results("exa_search", results)

    @property
    def last_credit_usage(self) -> dict[str, Any]:
        """Return the most recent local Exa request accounting context."""

        return dict(self._last_credit_usage)

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


@dataclass(frozen=True)
class TavilySearchProvider:
    """Tavily-backed search provider for AI-oriented web results."""

    live: bool = False
    api_key: str | None = None
    base_url: str | None = None
    search_depth: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    _last_credit_usage: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def dry_run(self) -> bool:
        return not self.live

    def _settings(self) -> Any:
        return load_settings()

    def _api_key(self) -> str:
        key = self.api_key or self._settings().tavily_api_key
        if not key:
            raise TavilyConfigurationError(
                "TAVILY_API_KEY is required for live Tavily search. "
                "Set it in the environment or .env, or run without --live-search."
            )
        return key

    def _base_url(self) -> str:
        return (self.base_url or self._settings().tavily_base_url).rstrip("/")

    def _search_depth(self) -> str:
        depth = (self.search_depth or self._settings().tavily_search_depth or "basic").lower()
        return depth if depth in {"ultra-fast", "fast", "basic", "advanced"} else "basic"

    def validate_configuration(self) -> None:
        """Validate live Tavily credentials without making a network call."""

        self._api_key()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        return self.search_structured(SearchRequest(query=query, num_results=num_results))

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="tavily_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("tavily_search", [])
        api_key = self._api_key()
        search_depth = self._search_depth()
        estimated_credits = tavily_search_credit_estimate(search_depth)
        decision = tavily_credit_preflight(estimated_credits)
        object.__setattr__(self, "_last_credit_usage", decision.to_dict())
        if not decision.allowed:
            raise TavilySearchError(decision.note)
        packet = _search_tavily_live(
            request=request,
            api_key=api_key,
            base_url=self._base_url(),
            search_depth=search_depth,
            estimated_credits=estimated_credits,
            timeout_seconds=self.timeout_seconds,
        )
        usage = record_tavily_credit_usage(
            credits=packet.credits_used,
            estimated_credits=estimated_credits,
            usage_source=packet.usage_source,
        )
        object.__setattr__(self, "_last_credit_usage", usage.to_dict())
        return _filter_unsafe_search_results("tavily_search", packet.results)

    @property
    def last_credit_usage(self) -> dict[str, Any]:
        """Return the most recent local Tavily credit context."""

        return dict(self._last_credit_usage)

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
class AgentsWebSearchProvider:
    """OpenAI Agents SDK hosted web-search provider for slow recall deepening."""

    live: bool = False
    search_context_size: str = "low"
    external_web_access: bool = True
    model: str | None = None
    runner: Callable[[str, SearchRequest], Any] | None = None
    _last_credit_usage: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def provider_name(self) -> str:
        return SearchProviderName.AGENTS_WEB_SEARCH.value

    @property
    def dry_run(self) -> bool:
        return not self.live

    def validate_configuration(self) -> None:
        """Validate hosted web-search prerequisites without making a model call."""

        if self.live and self.runner is None:
            self._validate_live_configuration()

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        return self.search_structured(SearchRequest(query=query, num_results=num_results))

    def search_structured(self, request: SearchRequest) -> list[SearchResult]:
        _guard_search_input(
            tool_name="agents_web_search",
            query=request.query,
            num_results=request.num_results,
            live=self.live,
        )
        if not self.live:
            return enforce_tool_output_guardrails("agents_web_search", [])
        prompt = _agents_web_search_prompt(request)
        if self.runner is not None:
            object.__setattr__(self, "_last_credit_usage", {})
            output = self.runner(prompt, request)
        else:
            self._validate_live_configuration()
            output = self._run_live(prompt)
        results = _agents_web_search_output_to_results(output, provider=self.provider_name)
        return _filter_unsafe_search_results("agents_web_search", results)

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
    ) -> list[SearchResult]:
        result_count = max_results if max_results is not None else num_results
        return self.search_web(query, num_results=result_count)

    def _validate_live_configuration(self) -> None:
        try:
            from keystone_agents.model_provider import DEFAULT_PROVIDER, get_model_config
            from keystone_agents.sdk import validate_web_search_sdk_available

            validate_web_search_sdk_available()
            model_config = get_model_config()
            if self.model is not None:
                model_config = replace(model_config, model=self.model)
            if model_config.provider != DEFAULT_PROVIDER or model_config.use_responses is False:
                raise AgentsWebSearchConfigurationError(
                    "Agents SDK hosted web search requires MODEL_PROVIDER=openai on the "
                    "OpenAI Responses path."
                )
            model_config.require_live_execution_ready()
        except AgentsWebSearchConfigurationError:
            raise
        except RuntimeError as exc:
            raise AgentsWebSearchConfigurationError(str(exc)) from exc

    def _run_live(self, prompt: str) -> Any:
        try:
            from keystone_agents.model_provider import get_model_config
            from keystone_agents.run import run_typed_sdk_agent
            from keystone_agents.sdk import WebSearchTool, build_sdk_agent

            model_config = get_model_config()
            if self.model is not None:
                model_config = replace(model_config, model=self.model)
            agent = build_sdk_agent(
                name="search_provider_agents_web_search",
                instructions=(
                    "You are a hosted web-search adapter for Keystone research agents. "
                    "Use web_search to return source-attributed search results. Prefer "
                    "primary and authoritative sources. Do not use authenticated, "
                    "private, or patient-specific pages."
                ),
                output_type=AgentsWebSearchOutput,
                tools=[
                    WebSearchTool(
                        search_context_size=self.search_context_size,
                        external_web_access=self.external_web_access,
                    )
                ],
                model=self.model,
            )
            run_result = run_typed_sdk_agent(
                agent=agent,
                typed_input=prompt,
                output_type=AgentsWebSearchOutput,
                live=True,
                config=model_config,
                workflow_name="Keystone hosted web search fallback",
                tracing_disabled=True,
                trace_include_sensitive_data=False,
            )
            usage = dict(run_result.usage or {})
            cost = dict(run_result.cost or {})
            object.__setattr__(
                self,
                "_last_credit_usage",
                {
                    "request_credits": 1,
                    "usage_source": "agents_sdk_usage",
                    "input_tokens": usage.get("input_tokens"),
                    "cached_input_tokens": usage.get("cached_input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
                    "cache_hit_rate": usage.get("cache_hit_rate"),
                    "estimated_usd": cost.get("estimated_usd", cost.get("amount_usd")),
                    "cost_source": cost.get("source"),
                },
            )
            return run_result.output
        except SearchProviderConfigurationError:
            raise
        except Exception as exc:
            raise AgentsWebSearchError(
                f"Agents SDK hosted web search failed: {type(exc).__name__}: {exc}"
            ) from exc

    def last_credit_usage(self) -> dict[str, Any]:
        return dict(self._last_credit_usage)


def build_search_provider(
    provider: str | SearchProviderName | None = None,
    *,
    live: bool = False,
    api_key: str | None = None,
    searxng_base_url: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> (
    DryRunSearchProvider
    | ExaSearchProvider
    | SerperSearchProvider
    | SearxngSearchProvider
    | FirecrawlSearchProvider
    | TavilySearchProvider
    | AgentsWebSearchProvider
):
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
            "Live search requires --search-provider exa, serper, searxng, firecrawl, "
            "tavily, or agents-web-search, or SEARCH_PROVIDER set to one of those values."
        )
    provider_name = normalize_search_provider_name(explicit_provider)
    if provider_name == SearchProviderName.DRY_RUN:
        raise LiveSearchProviderRequiredError(
            "Live search requires --search-provider exa, serper, searxng, firecrawl, "
            "tavily, or agents-web-search, or SEARCH_PROVIDER set to one of those values."
        )
    if provider_name == SearchProviderName.EXA:
        return ExaSearchProvider(
            live=True,
            api_key=api_key or settings.exa_api_key,
            base_url=settings.exa_base_url,
            timeout_seconds=timeout_seconds,
        )
    if provider_name == SearchProviderName.SERPER:
        if not settings.serper_enabled:
            raise SerperConfigurationError(
                "Serper search is disabled because API credits are unavailable. "
                "Use SearXNG, agents-web-search, Exa, or Tavily; set "
                "KEYSTONE_SERPER_ENABLED=true only after credits are restored."
            )
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
    if provider_name == SearchProviderName.TAVILY:
        return TavilySearchProvider(
            live=True,
            api_key=api_key or settings.tavily_api_key,
            base_url=settings.tavily_base_url,
            search_depth=settings.tavily_search_depth,
            timeout_seconds=timeout_seconds,
        )
    if provider_name == SearchProviderName.AGENTS_WEB_SEARCH:
        return AgentsWebSearchProvider(live=True)
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


def _search_exa_live(
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
    contents: dict[str, Any] = {"highlights": True}
    if request.scrape:
        contents["text"] = {"maxCharacters": 12000}
    payload: dict[str, Any] = {
        "query": resolved_query,
        "type": "auto",
        "numResults": min(request.num_results, 20),
        "contents": contents,
    }
    start_published_date = _exa_start_published_date(request.time_range)
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    try:
        response = post(
            urljoin(f"{base_url.rstrip('/')}/", EXA_SEARCH_PATH.lstrip("/")),
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.Timeout as exc:
        raise ExaSearchError(f"Exa search timed out after {timeout_seconds:.1f} seconds.") from exc
    except requests.RequestException as exc:
        raise ExaSearchError("Exa search request failed.") from exc

    data = _response_json(response, provider_name="Exa", error_type=ExaSearchError)
    results: list[SearchResult] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        result = _search_result_from_fields(
            title=item.get("title"),
            link=item.get("url"),
            snippet=_exa_snippet(item),
            source="exa",
            date=item.get("publishedDate") or item.get("published_date"),
            content=item.get("text"),
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
        raise SearxngSearchError(
            f"SearXNG search request failed for {base_url.rstrip('/')}/search: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

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
    if not results:
        unresponsive_summary = _searxng_unresponsive_engine_summary(
            data.get("unresponsive_engines")
        )
        if unresponsive_summary:
            raise SearxngSearchError(
                "SearXNG returned no results while search engines were unavailable: "
                f"{unresponsive_summary}."
            )
    return results


def _searxng_unresponsive_engine_summary(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    summaries: list[str] = []
    for item in value[:8]:
        if isinstance(item, list | tuple) and item:
            engine = str(item[0] or "unknown").strip()
            reason = str(item[1] if len(item) > 1 else "unavailable").strip()
            summaries.append(f"{engine} ({reason})")
        elif isinstance(item, dict):
            engine = str(item.get("engine") or item.get("name") or "unknown").strip()
            reason = str(item.get("error") or item.get("reason") or "unavailable").strip()
            summaries.append(f"{engine} ({reason})")
    return ", ".join(item for item in summaries if item)


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


def _agents_web_search_prompt(request: SearchRequest) -> str:
    parts = [
        "Find web search results for this Keystone search request.",
        f"Query: {request.query}",
        f"Return up to {request.num_results} distinct results.",
        "Each result must include title, URL, a concise relevance snippet, and date if visible.",
        "Prefer primary-source domains when available. Preserve canonical source URLs.",
        "Do not include authenticated, private, or patient-specific pages.",
    ]
    if request.time_range:
        parts.append(f"Requested time range hint: {request.time_range}.")
    if request.source and request.source != "web":
        parts.append(f"Requested source hint: {request.source}.")
    return "\n".join(parts)


def _search_tavily_live(
    *,
    request: SearchRequest,
    api_key: str,
    base_url: str,
    search_depth: str,
    estimated_credits: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    http_post: Callable[..., Any] | None = None,
) -> TavilySearchPacket:
    resolved_query = _validate_query(request.query, request.num_results)
    if not resolved_query:
        return TavilySearchPacket(results=[], credits_used=0, usage_source="empty_query")

    post = http_post or requests.post
    payload: dict[str, Any] = {
        "query": resolved_query,
        "max_results": min(request.num_results, 20),
        "search_depth": search_depth,
        "topic": "news" if _normalized_source(request.source) == "news" else "general",
        "include_answer": False,
        "include_images": False,
        "include_raw_content": "markdown" if request.scrape else False,
        "include_usage": True,
    }
    time_range = _tavily_time_range(request.time_range)
    if time_range:
        payload["time_range"] = time_range
    country = _tavily_country(request.country)
    if country and payload["topic"] == "general":
        payload["country"] = country
    try:
        response = post(
            f"{base_url.rstrip('/')}/search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.Timeout as exc:
        raise TavilySearchError(
            f"Tavily search timed out after {timeout_seconds:.1f} seconds."
        ) from exc
    except requests.RequestException as exc:
        raise TavilySearchError("Tavily search request failed.") from exc

    data = _response_json(response, provider_name="Tavily", error_type=TavilySearchError)
    results: list[SearchResult] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        raw_content = item.get("raw_content")
        result = _search_result_from_fields(
            title=item.get("title"),
            link=item.get("url"),
            snippet=item.get("content"),
            source="tavily",
            date=item.get("published_date") or item.get("date"),
            content=raw_content if raw_content else None,
        )
        if result is not None:
            results.append(result)
        if len(results) >= request.num_results:
            break
    credits_used = _tavily_response_credits(data, default=estimated_credits)
    usage_source = "provider_response" if _tavily_response_credits(data, default=0) else "estimate"
    return TavilySearchPacket(
        results=results,
        credits_used=credits_used,
        usage_source=usage_source,
    )


def _tavily_response_credits(data: dict[str, Any], *, default: int) -> int:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return default
    try:
        credits = int(float(usage.get("credits") or 0))
    except (TypeError, ValueError):
        return default
    return credits if credits > 0 else default


def _tavily_time_range(time_range: str | None) -> str | None:
    normalized = str(time_range or "").strip().lower()
    mapping = {
        "day": "day",
        "d": "day",
        "week": "week",
        "w": "week",
        "recent": "month",
        "month": "month",
        "m": "month",
        "current": "year",
        "year": "year",
        "y": "year",
    }
    return mapping.get(normalized)


def _tavily_country(country: str | None) -> str | None:
    normalized = str(country or "").strip().lower()
    mapping = {
        "us": "united states",
        "usa": "united states",
        "united states of america": "united states",
        "uk": "united kingdom",
    }
    return mapping.get(normalized, normalized or None)


def _exa_start_published_date(time_range: str | None) -> str | None:
    normalized = str(time_range or "").strip().lower()
    days_by_range = {
        "day": 1,
        "d": 1,
        "week": 7,
        "w": 7,
        "recent": 31,
        "month": 31,
        "m": 31,
        "current": 366,
        "year": 366,
        "y": 366,
    }
    days = days_by_range.get(normalized)
    if days is None:
        return None
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def _exa_snippet(item: dict[str, Any]) -> str:
    highlights = item.get("highlights")
    if isinstance(highlights, list):
        joined = " ".join(str(highlight).strip() for highlight in highlights if highlight)
        if joined.strip():
            return joined[:1000]
    summary = item.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:1000]
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()[:1000]
    return str(item.get("snippet") or "").strip()


def _agents_web_search_output_to_results(output: Any, *, provider: str) -> list[SearchResult]:
    parsed = _coerce_agents_web_search_output(output)
    results: list[SearchResult] = []
    for item in parsed.results:
        url = str(item.url or "").strip()
        if not url:
            continue
        title = str(item.title or "").strip() or url
        results.append(
            SearchResult(
                title=title,
                link=url,
                snippet=str(item.snippet or "").strip(),
                source=provider,
                date=item.date,
            )
        )
    return results


def _coerce_agents_web_search_output(output: Any) -> AgentsWebSearchOutput:
    if hasattr(output, "final_output"):
        output = output.final_output
    if isinstance(output, AgentsWebSearchOutput):
        return output
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return AgentsWebSearchOutput()
    if isinstance(output, list | tuple):
        output = {"results": list(output)}
    if isinstance(output, dict):
        if "results" not in output and "items" in output:
            output = {**output, "results": output["items"]}
        return AgentsWebSearchOutput.model_validate(output)
    return AgentsWebSearchOutput()


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
