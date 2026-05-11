"""Compatibility wrappers for Serper search and the SDK search tool."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keystone_agents.config import parse_bool
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.tools.search_provider import (
    DEFAULT_TIMEOUT_SECONDS,
    SERPER_SEARCH_URL,
    FirecrawlConfigurationError,
    FirecrawlSearchError,
    FirecrawlSearchProvider,
    SearchProviderConfigurationError,
    SearchProviderError,
    SearchProviderName,
    SearchRequest,
    SearchResult,
    SearxngConfigurationError,
    SearxngSearchError,
    SearxngSearchProvider,
    SerperConfigurationError,
    SerperSearchError,
    SerperSearchProvider,
    build_search_provider,
    normalize_search_provider_name,
    requests,
)


@dataclass(frozen=True)
class SerperTool:
    """Small Serper client preserved for existing call sites."""

    live: bool = False
    api_key: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def search(
        self,
        query: str,
        num_results: int = 5,
        *,
        max_results: int | None = None,
        http_post: Callable[..., Any] | None = None,
    ) -> list[SearchResult] | dict[str, Any]:
        """Search Serper when live mode is enabled, otherwise return no results."""

        return SerperSearchProvider(
            live=self.live,
            api_key=self.api_key,
            timeout_seconds=self.timeout_seconds,
        ).search(
            query=query,
            num_results=num_results,
            max_results=max_results,
            http_post=http_post,
        )


@function_tool(**keystone_tool_guardrail_kwargs())
def search_web(query: str, num_results: int = 5) -> list[SearchResult]:
    """SDK search tool with safe defaults and env-gated live retrieval.

    By default this tool is inert. When the operator explicitly enables live research through the
    Keystone environment flags, it follows the live search ladder for SDK runs:
    SearXNG broad recall first, Serper only when `SEARCH_PROVIDER=serper` is set explicitly.
    """

    provider_name = _sdk_live_search_provider_name()
    provider = build_search_provider(
        provider=provider_name,
        live=provider_name is not None,
    )
    return provider.search_web(query=query, num_results=num_results)


def _sdk_live_search_provider_name() -> SearchProviderName | None:
    """Resolve the live SDK search provider from environment policy."""

    raw_live_mode = os.getenv("KEYSTONE_LIVE_MODE")
    if raw_live_mode is None or not parse_bool(raw_live_mode):
        return None
    raw_dry_run = os.getenv("KEYSTONE_DRY_RUN")
    if raw_dry_run is not None and parse_bool(raw_dry_run):
        return None
    raw_live_research = os.getenv("KEYSTONE_ENABLE_LIVE_RESEARCH")
    if raw_live_research is None and raw_dry_run is None:
        return None
    if raw_live_research is not None and not parse_bool(raw_live_research):
        return None
    raw_provider = os.getenv("SEARCH_PROVIDER")
    if raw_provider is None or not raw_provider.strip():
        return SearchProviderName.SEARXNG
    provider_name = normalize_search_provider_name(raw_provider)
    if provider_name == SearchProviderName.DRY_RUN:
        return None
    return provider_name


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SERPER_SEARCH_URL",
    "FirecrawlConfigurationError",
    "FirecrawlSearchError",
    "FirecrawlSearchProvider",
    "SearchProviderConfigurationError",
    "SearchProviderError",
    "SearchProviderName",
    "SearchRequest",
    "SearchResult",
    "SearxngConfigurationError",
    "SearxngSearchError",
    "SearxngSearchProvider",
    "SerperConfigurationError",
    "SerperSearchError",
    "SerperSearchProvider",
    "SerperTool",
    "build_search_provider",
    "normalize_search_provider_name",
    "requests",
    "search_web",
]
