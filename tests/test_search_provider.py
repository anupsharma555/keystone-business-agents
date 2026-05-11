from __future__ import annotations

import pytest

from keystone_agents.config import Settings
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.tools.search_provider import (
    DryRunSearchProvider,
    FirecrawlSearchProvider,
    LiveSearchProviderRequiredError,
    SearchProviderName,
    SearchRequest,
    SearchResult,
    SearxngConfigurationError,
    SearxngSearchProvider,
    SerperConfigurationError,
    SerperSearchProvider,
    build_search_provider,
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


def test_pytest_search_paths_make_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")

    def fail_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pytest search paths must not call the network")

    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.post", fail_network)
    monkeypatch.setattr("keystone_agents.tools.search_provider.requests.get", fail_network)

    assert build_search_provider("serper", live=False).search_web("Curebase", 1) == []
    assert build_search_provider("searxng", live=False).search_web("Curebase", 1) == []
    assert search_web("Curebase", num_results=1) == []
