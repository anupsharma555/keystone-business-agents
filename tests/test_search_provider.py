from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import requests

from keystone_agents.config import Settings
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools.search_provider import (
    AgentsWebSearchOutput,
    AgentsWebSearchProvider,
    AgentsWebSearchResult,
    DryRunSearchProvider,
    ExaConfigurationError,
    ExaSearchProvider,
    FirecrawlSearchProvider,
    LiveSearchProviderRequiredError,
    SearchProviderName,
    SearchRequest,
    SearchResult,
    SearxngConfigurationError,
    SearxngSearchError,
    SearxngSearchProvider,
    SerperConfigurationError,
    SerperSearchProvider,
    TavilyConfigurationError,
    TavilySearchError,
    TavilySearchProvider,
    build_search_provider,
    normalize_search_provider_name,
)
from keystone_agents.tools.serper_tool import search_web


class SerperResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "organic": [
                {
                    "title": "Curebase clinical trial software",
                    "link": "https://example.com/curebase",
                    "snippet": "Curebase supports decentralized clinical trial workflows.",
                }
            ]
        }


class SerperMixedSafetyResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "organic": [
                {
                    "title": "Patient Alex case note",
                    "link": "https://example.com/patient-case",
                    "snippet": "Patient Alex has a depression diagnosis and needs treatment.",
                },
                {
                    "title": "Remote clinical AI medical director",
                    "link": "https://example.com/clinical-ai-role",
                    "snippet": "Remote role focused on clinical AI validation.",
                },
            ]
        }


class SerperAggregateFalsePositiveResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "organic": [
                {
                    "title": "Patient Engagement Platform",
                    "link": "https://example.com/patient-engagement",
                    "snippet": "Remote clinical AI role.",
                },
                {
                    "title": "Depression research jobs",
                    "link": "https://example.com/depression-research",
                    "snippet": "Remote medical director opportunity.",
                },
            ]
        }


class SearxngResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "Curebase clinical trial software",
                    "url": "https://example.com/curebase",
                    "content": "Curebase supports decentralized clinical trial workflows.",
                }
            ]
        }


class SearxngMixedSafetyResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "Patient Alex case note",
                    "url": "https://example.com/patient-case",
                    "content": "Patient Alex has a depression diagnosis and needs treatment.",
                },
                {
                    "title": "Remote clinical AI medical director",
                    "url": "https://example.com/clinical-ai-role",
                    "content": "Remote role focused on clinical AI validation.",
                },
            ]
        }


class ExaSearchResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "Mentavi Health careers",
                    "url": "https://mentavi.com/careers/",
                    "publishedDate": "2026-05-01T00:00:00.000Z",
                    "highlights": ["Official careers page for Mentavi Health."],
                    "text": "Full careers page text.",
                }
            ]
        }


class FirecrawlSearchResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "news": [
                    {
                        "title": "Behavioral health AI partnership",
                        "url": "https://example.com/news",
                        "snippet": (
                            "A behavioral health AI company announced a clinical partnership."
                        ),
                        "date": "2026-04-20",
                        "markdown": "Full verified article text.",
                    }
                ]
            },
        }


class TavilySearchResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "results": [
                {
                    "title": "Mentavi Health careers",
                    "url": "https://mentavi.com/careers/",
                    "content": "Official careers page for Mentavi Health.",
                    "raw_content": "Full careers page text.",
                    "published_date": "2026-05-01",
                }
            ],
            "response_time": "0.80",
            "usage": {"credits": 1},
        }


def test_build_search_provider_defaults_to_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)

    provider = build_search_provider(live=False)

    assert isinstance(provider, DryRunSearchProvider)
    assert provider.provider_name == SearchProviderName.DRY_RUN.value
    assert provider.dry_run is True


def test_live_search_requires_named_live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(search_provider="dry-run"),
    )

    with pytest.raises(LiveSearchProviderRequiredError, match="Live search requires"):
        build_search_provider(live=True)


def test_invalid_search_provider_name_fails_clearly() -> None:
    with pytest.raises(ValueError, match="search provider must be one of"):
        build_search_provider("not-a-search-provider", live=False)


def test_search_provider_normalizer_accepts_agents_web_search_aliases() -> None:
    assert normalize_search_provider_name("exa") == SearchProviderName.EXA
    assert normalize_search_provider_name("tavily-search") == SearchProviderName.TAVILY
    assert (
        normalize_search_provider_name("agents-web-search") == SearchProviderName.AGENTS_WEB_SEARCH
    )
    assert (
        normalize_search_provider_name("openai-web-search") == SearchProviderName.AGENTS_WEB_SEARCH
    )
    assert (
        normalize_search_provider_name("native_web_search") == SearchProviderName.AGENTS_WEB_SEARCH
    )


def test_live_search_uses_configured_provider_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(
            search_provider="searxng",
            searxng_base_url="http://127.0.0.1:8080",
        ),
    )

    provider = build_search_provider(live=True)

    assert isinstance(provider, SearxngSearchProvider)
    provider.validate_configuration()


def test_build_search_provider_can_build_agents_web_search_provider() -> None:
    dry_provider = build_search_provider("agents-web-search", live=False)
    live_provider = build_search_provider("agents-web-search", live=True)

    assert isinstance(dry_provider, DryRunSearchProvider)
    assert dry_provider.provider_name == "agents-web-search"
    assert isinstance(live_provider, AgentsWebSearchProvider)


def test_build_search_provider_can_build_tavily_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(tavily_api_key="test-tavily-key"),
    )

    dry_provider = build_search_provider("tavily", live=False)
    live_provider = build_search_provider("tavily", live=True)

    assert isinstance(dry_provider, DryRunSearchProvider)
    assert dry_provider.provider_name == "tavily"
    assert isinstance(live_provider, TavilySearchProvider)


def test_build_search_provider_can_build_exa_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(exa_api_key="test-exa-key"),
    )

    dry_provider = build_search_provider("exa", live=False)
    live_provider = build_search_provider("exa", live=True)

    assert isinstance(dry_provider, DryRunSearchProvider)
    assert dry_provider.provider_name == "exa"
    assert isinstance(live_provider, ExaSearchProvider)
    live_provider.validate_configuration()


def test_agents_web_search_provider_normalizes_runner_output() -> None:
    provider = AgentsWebSearchProvider(
        live=True,
        runner=lambda _prompt, _request: AgentsWebSearchOutput(
            results=[
                AgentsWebSearchResult(
                    title="Mentavi careers",
                    url="https://mentavi.com/careers/",
                    snippet="Official careers page.",
                    date="last week",
                )
            ]
        ),
    )

    assert provider.search_web("Mentavi careers", num_results=1) == [
        SearchResult(
            title="Mentavi careers",
            link="https://mentavi.com/careers/",
            snippet="Official careers page.",
            source="agents-web-search",
            date="last week",
        )
    ]


def test_search_provider_guardrails_reject_sensitive_queries() -> None:
    provider = build_search_provider(live=False)

    with pytest.raises(ToolGuardrailViolation, match="secret-like content"):
        provider.search_web("api_key=SHOULD_NOT_APPEAR_123456789", num_results=1)


def test_live_search_guardrails_reject_unsafe_query_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("guardrail rejection must happen before network")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)

    with pytest.raises(ToolGuardrailViolation, match="secret-like content"):
        SerperSearchProvider(live=True, api_key="test-key").search_web(
            "api_key=REDACTED_TEST_SECRET",
            num_results=1,
        )


def test_live_search_guardrails_reject_patient_specific_query_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("guardrail rejection must happen before network")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)

    with pytest.raises(ToolGuardrailViolation, match="possible PHI"):
        SerperSearchProvider(live=True, api_key="test-key").search_web(
            "Patient Alex depression diagnosis",
            num_results=1,
        )


def test_serper_provider_parses_mocked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return SerperResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    provider = SerperSearchProvider(live=True, api_key="test-key", timeout_seconds=3.0)
    results = provider.search_web("Curebase clinical trial software", num_results=1)

    assert results == [
        SearchResult(
            title="Curebase clinical trial software",
            link="https://example.com/curebase",
            snippet="Curebase supports decentralized clinical trial workflows.",
            source="serper",
        )
    ]
    assert calls[0]["json"] == {"q": "Curebase clinical trial software", "num": 1}
    assert calls[0]["timeout"] == 3.0


def test_exa_provider_requires_key_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fail_network(*args: object, **_kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("live credential test must not make a network request")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    with pytest.raises(ExaConfigurationError, match="EXA_API_KEY is required"):
        ExaSearchProvider(live=True).search_web("Mentavi Health", num_results=1)

    assert calls == []


def test_exa_provider_parses_highlights_and_sends_bounded_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return ExaSearchResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    provider = ExaSearchProvider(
        live=True,
        api_key="test-exa-key",
        base_url="https://api.exa.ai",
        timeout_seconds=4.0,
    )
    results = provider.search_structured(
        SearchRequest(query="Mentavi Health careers", num_results=1, scrape=True)
    )

    assert results == [
        SearchResult(
            title="Mentavi Health careers",
            link="https://mentavi.com/careers/",
            snippet="Official careers page for Mentavi Health.",
            source="exa",
            date="2026-05-01T00:00:00.000Z",
            content="Full careers page text.",
        )
    ]
    assert calls[0]["url"] == "https://api.exa.ai/search"
    assert calls[0]["headers"]["x-api-key"] == "test-exa-key"
    assert calls[0]["json"] == {
        "query": "Mentavi Health careers",
        "type": "auto",
        "numResults": 1,
        "contents": {"highlights": True, "text": {"maxCharacters": 12000}},
    }
    assert calls[0]["timeout"] == 4.0
    assert provider.last_credit_usage == {
        "request_credits": 1,
        "usage_source": "provider_request_estimate",
    }


def test_serper_structured_search_passes_recency_geo_and_news_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return type(
            "Response",
            (),
            {
                "status_code": 200,
                "json": lambda self: {
                    "news": [
                        {
                            "title": "Curebase news",
                            "link": "https://example.com/news",
                            "snippet": "Current news.",
                            "date": "2026-04-20",
                        }
                    ]
                },
            },
        )()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    results = SerperSearchProvider(live=True, api_key="test-key").search_structured(
        SearchRequest(
            query="behavioral health AI partnership",
            num_results=1,
            source="news",
            time_range="week",
            country="US",
            location="United States",
            language="en",
        )
    )

    assert results[0].date == "2026-04-20"
    assert calls[0]["url"].endswith("/news")
    assert calls[0]["json"] == {
        "q": "behavioral health AI partnership",
        "num": 1,
        "hl": "en",
        "gl": "us",
        "location": "United States",
        "tbs": "qdr:w",
    }


def test_serper_provider_filters_unsafe_search_result_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.requests.post",
        lambda *_args, **_kwargs: SerperMixedSafetyResponse(),
    )

    results = SerperSearchProvider(live=True, api_key="test-key").search_web(
        "clinical AI remote medical director",
        num_results=2,
    )

    assert results == [
        SearchResult(
            title="Remote clinical AI medical director",
            link="https://example.com/clinical-ai-role",
            snippet="Remote role focused on clinical AI validation.",
            source="serper",
        )
    ]


def test_serper_provider_does_not_cross_match_unrelated_safe_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.requests.post",
        lambda *_args, **_kwargs: SerperAggregateFalsePositiveResponse(),
    )

    results = SerperSearchProvider(live=True, api_key="test-key").search_web(
        "clinical AI remote medical director",
        num_results=2,
    )

    assert results == [
        SearchResult(
            title="Patient Engagement Platform",
            link="https://example.com/patient-engagement",
            snippet="Remote clinical AI role.",
            source="serper",
        ),
        SearchResult(
            title="Depression research jobs",
            link="https://example.com/depression-research",
            snippet="Remote medical director opportunity.",
            source="serper",
        ),
    ]


def test_searxng_provider_parses_mocked_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, *, headers: dict[str, str], params: dict[str, str], timeout: float):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return SearxngResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.get", fake_get)

    provider = SearxngSearchProvider(
        live=True,
        base_url="http://127.0.0.1:8080",
        timeout_seconds=2.0,
    )
    results = provider.search_web("Curebase clinical trial software", num_results=1)

    assert results == [
        SearchResult(
            title="Curebase clinical trial software",
            link="https://example.com/curebase",
            snippet="Curebase supports decentralized clinical trial workflows.",
            source="searxng",
        )
    ]
    assert calls[0]["url"] == "http://127.0.0.1:8080/search"
    assert calls[0]["params"] == {"q": "Curebase clinical trial software", "format": "json"}
    assert calls[0]["timeout"] == 2.0


def test_searxng_provider_reports_request_endpoint_and_exception_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(*_args: object, **_kwargs: object) -> object:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.get", fake_get)

    provider = SearxngSearchProvider(
        live=True,
        base_url="http://127.0.0.1:18080",
        timeout_seconds=2.0,
    )

    with pytest.raises(SearxngSearchError) as excinfo:
        provider.search_web("clinical AI safety", num_results=1)

    message = str(excinfo.value)
    assert "http://127.0.0.1:18080/search" in message
    assert "ConnectionError" in message
    assert "connection refused" in message


def test_searxng_structured_search_passes_categories_time_language_and_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(url: str, *, headers: dict[str, str], params: dict[str, str], timeout: float):
        calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return SearxngResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.get", fake_get)

    SearxngSearchProvider(live=True, base_url="http://127.0.0.1:8080").search_structured(
        SearchRequest(
            query="clinical AI advisory",
            num_results=1,
            source="news",
            time_range="week",
            language="en-US",
            page=2,
            safe_search=1,
        )
    )

    assert calls[0]["params"] == {
        "q": "clinical AI advisory",
        "format": "json",
        "categories": "news",
        "language": "en-US",
        "pageno": "2",
        "time_range": "month",
        "safesearch": "1",
    }


def test_firecrawl_provider_parses_search_response_and_sends_search_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FirecrawlSearchResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    results = FirecrawlSearchProvider(
        live=True,
        api_key="test-key",
        base_url="https://api.firecrawl.dev",
        timeout_seconds=6.0,
    ).search_structured(
        SearchRequest(
            query="psychiatry AI advisory",
            num_results=1,
            source="news",
            time_range="month",
            country="US",
            location="United States",
            scrape=True,
        )
    )

    assert results == [
        SearchResult(
            title="Behavioral health AI partnership",
            link="https://example.com/news",
            snippet="A behavioral health AI company announced a clinical partnership.",
            source="firecrawl",
            date="2026-04-20",
            content="Full verified article text.",
        )
    ]
    assert calls[0]["url"] == "https://api.firecrawl.dev/v2/search"
    assert calls[0]["json"] == {
        "query": "psychiatry AI advisory",
        "limit": 1,
        "sources": ["news"],
        "timeout": 6000,
        "ignoreInvalidURLs": True,
        "tbs": "qdr:m",
        "location": "United States",
        "country": "US",
        "scrapeOptions": {
            "formats": ["markdown"],
            "onlyMainContent": True,
            "removeBase64Images": True,
            "timeout": 6000,
        },
    }


def test_tavily_structured_search_uses_basic_depth_and_parses_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return TavilySearchResponse()

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    provider = TavilySearchProvider(
        live=True,
        api_key="test-tavily-key",
        base_url="https://api.tavily.com",
        search_depth="basic",
        timeout_seconds=4.0,
    )
    results = provider.search_structured(
        SearchRequest(
            query="Mentavi Health careers",
            num_results=1,
            time_range="month",
            country="US",
            scrape=True,
        )
    )

    assert results == [
        SearchResult(
            title="Mentavi Health careers",
            link="https://mentavi.com/careers/",
            snippet="Official careers page for Mentavi Health.",
            source="tavily",
            date="2026-05-01",
            content="Full careers page text.",
        )
    ]
    assert calls[0]["url"] == "https://api.tavily.com/search"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-tavily-key"
    assert calls[0]["json"] == {
        "query": "Mentavi Health careers",
        "max_results": 1,
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
        "include_images": False,
        "include_raw_content": "markdown",
        "include_usage": True,
        "time_range": "month",
        "country": "united states",
    }
    assert provider.last_credit_usage["request_credits"] == 1
    assert provider.last_credit_usage["status"] == "ok"


def test_tavily_structured_search_tracks_provider_reported_credits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    usage_path = tmp_path / "tavily_usage.json"

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        return TavilySearchResponse()

    monkeypatch.setenv("KEYSTONE_TAVILY_USAGE_PATH", str(usage_path))
    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    provider = TavilySearchProvider(
        live=True,
        api_key="test-tavily-key",
        base_url="https://api.tavily.com",
        search_depth="basic",
    )
    provider.search_web("Mentavi Health careers", num_results=1)

    current_month = datetime.now(UTC).strftime("%Y-%m")
    stored = json.loads(usage_path.read_text(encoding="utf-8"))
    assert stored["months"][current_month]["credits"] == 1
    assert provider.last_credit_usage["request_credits"] == 1
    assert provider.last_credit_usage["observed_monthly_credits"] == 1
    assert provider.last_credit_usage["status"] == "ok"


def test_tavily_credit_budget_can_block_when_monthly_limit_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    usage_path = tmp_path / "tavily_usage.json"
    current_month = datetime.now(UTC).strftime("%Y-%m")
    usage_path.write_text(
        json.dumps({"months": {current_month: {"credits": 1}}}),
        encoding="utf-8",
    )
    calls: list[object] = []

    def fake_post(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))
        raise AssertionError("credit budget block should happen before network")

    monkeypatch.setenv("KEYSTONE_TAVILY_USAGE_PATH", str(usage_path))
    monkeypatch.setenv("KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT", "1")
    monkeypatch.setenv("KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT", "1")
    monkeypatch.setenv("KEYSTONE_TAVILY_CREDIT_ENFORCEMENT", "block")
    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fake_post)

    provider = TavilySearchProvider(
        live=True,
        api_key="test-tavily-key",
        base_url="https://api.tavily.com",
        search_depth="basic",
    )

    with pytest.raises(TavilySearchError, match="above the configured limit"):
        provider.search_web("Mentavi Health careers", num_results=1)

    assert calls == []
    assert provider.last_credit_usage["allowed"] is False


def test_searxng_provider_filters_unsafe_search_result_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.requests.get",
        lambda *_args, **_kwargs: SearxngMixedSafetyResponse(),
    )

    results = SearxngSearchProvider(
        live=True,
        base_url="http://127.0.0.1:8080",
    ).search_web(
        "clinical AI remote medical director",
        num_results=2,
    )

    assert results == [
        SearchResult(
            title="Remote clinical AI medical director",
            link="https://example.com/clinical-ai-role",
            snippet="Remote role focused on clinical AI validation.",
            source="searxng",
        )
    ]


def test_searxng_live_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(searxng_base_url=None),
    )

    with pytest.raises(SearxngConfigurationError, match="SEARXNG_BASE_URL is required"):
        SearxngSearchProvider(live=True).search_web("Curebase", num_results=1)


def test_serper_live_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    with pytest.raises(SerperConfigurationError, match="SERPER_API_KEY is required"):
        SerperSearchProvider(live=True).search_web("Curebase", num_results=1)


def test_tavily_live_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(
        "keystone_agents.tools.search_provider.load_settings",
        lambda: Settings(tavily_api_key=None),
    )

    with pytest.raises(TavilyConfigurationError, match="TAVILY_API_KEY is required"):
        TavilySearchProvider(live=True).search_web("Curebase", num_results=1)


def test_pytest_search_paths_make_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pytest search paths must not call the network")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)
    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.get", fail_network)

    assert build_search_provider("serper", live=False).search_web("Curebase", 1) == []
    assert build_search_provider("searxng", live=False).search_web("Curebase", 1) == []
    assert build_search_provider("tavily", live=False).search_web("Curebase", 1) == []
    assert search_web("Curebase", num_results=1) == []
