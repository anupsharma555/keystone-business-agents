from __future__ import annotations

import pytest
import requests

from keystone_agents.tools.serper_tool import (
    SearchResult,
    SerperConfigurationError,
    SerperSearchError,
    SerperTool,
    consume_sdk_search_telemetry,
    reset_sdk_search_telemetry,
    sdk_search_diagnostics_from_telemetry,
    search_web,
    set_sdk_search_request_context,
)


class MockResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        return {
            "organic": [
                {
                    "title": "Curebase clinical trial software",
                    "link": "https://example.com/curebase",
                    "snippet": "Curebase supports decentralized clinical trial workflows.",
                },
                {
                    "title": "Curebase funding news",
                    "link": "https://example.com/curebase-funding",
                    "snippet": "Recent company news mentions funding and product expansion.",
                },
            ]
        }


def test_mocked_serper_response_produces_search_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, object], timeout: float):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return MockResponse()

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fake_post)

    results = SerperTool(live=True, api_key="test-key", timeout_seconds=3.0).search(
        "Curebase clinical trial software",
        num_results=2,
    )

    assert results == [
        SearchResult(
            title="Curebase clinical trial software",
            link="https://example.com/curebase",
            snippet="Curebase supports decentralized clinical trial workflows.",
            source="serper",
        ),
        SearchResult(
            title="Curebase funding news",
            link="https://example.com/curebase-funding",
            snippet="Recent company news mentions funding and product expansion.",
            source="serper",
        ),
    ]
    assert calls[0]["json"] == {"q": "Curebase clinical trial software", "num": 2}
    assert calls[0]["timeout"] == 3.0


def test_missing_serper_api_key_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    with pytest.raises(SerperConfigurationError, match="SERPER_API_KEY is required"):
        SerperTool(live=True).search("Curebase", num_results=1)


def test_dry_serper_tool_makes_no_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pytest must not make live Serper calls")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)

    assert SerperTool(live=False).search("Curebase", num_results=1) == []


def test_dry_serper_legacy_response_makes_no_live_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("dry legacy Serper search must not make live calls")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)

    assert SerperTool(live=False).search("Curebase", max_results=2) == {
        "status": "dry-run",
        "query": "Curebase",
        "results": [],
    }


def test_search_web_is_inert_even_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)
    monkeypatch.delenv("KEYSTONE_DRY_RUN", raising=False)
    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_RESEARCH", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")

    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("search_web must not make network calls")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)

    assert search_web("Curebase", num_results=1) == []


def test_search_web_defaults_to_live_searxng_when_live_research_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")

    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default live SDK search should prefer SearXNG before Serper")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.requests.get",
        lambda *_args, **_kwargs: SearxngResponse(),
    )

    assert search_web("Curebase", num_results=1) == [
        SearchResult(
            title="Curebase clinical trial software",
            link="https://example.com/curebase",
            snippet="Curebase supports decentralized clinical trial workflows.",
            source="searxng",
        )
    ]


def test_search_web_default_live_policy_includes_hosted_agents_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

        def search_web(self, query: str, num_results: int = 5):
            calls.append(f"{self.provider_name}:{query}:{num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    results = search_web("Curebase", num_results=2)

    assert {
        "searxng:Curebase:2",
        "agents-web-search:Curebase:2",
    } <= set(calls)
    assert [result.source for result in results] == ["searxng", "agents-web-search"]


def test_search_web_default_live_policy_can_include_exa_deepening_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    results = search_web("Curebase", num_results=2)

    assert "searxng:Curebase:2" in calls
    assert "agents-web-search:Curebase:2" in calls
    assert any(
        call.startswith("exa:Curebase related organizations primary sources ")
        for call in calls
    )
    assert [result.source for result in results] == [
        "searxng",
        "agents-web-search",
        "exa",
    ]


def test_search_web_default_live_policy_can_include_tavily_deepening_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    results = search_web("Curebase", num_results=2)

    assert "searxng:Curebase:2" in calls
    assert "agents-web-search:Curebase:2" in calls
    assert any(call.startswith("tavily:Curebase official primary sources ") for call in calls)
    assert [result.source for result in results] == [
        "searxng",
        "agents-web-search",
        "tavily",
    ]


def test_search_web_can_query_trigger_tavily_deepening_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_MAX_CALLS_PER_RUN", "1")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    results = search_web("Run deeper search and compare providers for Curebase", num_results=2)

    assert any(call.startswith("tavily:") for call in calls)
    assert [result.source for result in results] == [
        "searxng",
        "agents-web-search",
        "tavily",
    ]


def test_search_web_can_context_trigger_tavily_deepening_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    set_sdk_search_request_context("Search mode: deeper provider comparison.")
    results = search_web("CalMHSA behavioral health clinical AI tools RFP", num_results=2)

    assert any(call.startswith("tavily:") for call in calls)
    assert [result.source for result in results] == [
        "searxng",
        "agents-web-search",
        "tavily",
    ]


def test_search_web_can_context_trigger_tavily_from_deepened_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-key")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    set_sdk_search_request_context("Please search beyond a first pass with a deepened brief.")
    results = search_web("OpenAI mental health official", num_results=2)

    assert any(call.startswith("tavily:") for call in calls)
    assert [result.source for result in results] == [
        "searxng",
        "agents-web-search",
        "tavily",
    ]


def test_search_web_natural_exa_preference_falls_back_after_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(self.provider_name)
            if self.provider_name == "exa":
                raise TimeoutError("synthetic Exa timeout")
            return [
                SearchResult(
                    title=f"{request.query} — {self.provider_name} result {index}",
                    link=f"https://source{index}.example/about",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
                for index in range(4)
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    set_sdk_search_request_context("Use Exa to research this company, with safe backups.")
    results = search_web("bounded company query", num_results=2)
    diagnostics = sdk_search_diagnostics_from_telemetry(consume_sdk_search_telemetry())

    assert calls == ["exa", "agents-web-search"]
    assert {result.source for result in results} == {"agents-web-search"}
    assert diagnostics["search_provider_sequence"] == [
        "exa",
        "agents-web-search",
        "searxng",
    ]
    assert diagnostics["provider_error_fallback_used"] is True


def test_search_web_natural_firecrawl_preference_stays_primary_when_useful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(self.provider_name)
            return [
                SearchResult(
                    title=f"{request.query} — Firecrawl source {index}",
                    link=f"https://source{index}.example/about",
                    snippet="Official source-backed result.",
                    source=self.provider_name,
                )
                for index in range(4)
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "false")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    set_sdk_search_request_context("Try Firecrawl for this public-source search.")
    results = search_web("official company source", num_results=4)
    consume_sdk_search_telemetry()

    assert calls == ["firecrawl"]
    assert {result.source for result in results} == {"firecrawl"}


def test_search_web_enforces_optional_provider_caps_across_sdk_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            calls.append(f"{self.provider_name}:{request.query}:{request.num_results}")
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "false")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_MAX_CALLS_PER_RUN", "2")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    set_sdk_search_request_context("Run a deepened search beyond a first pass.")
    for index in range(3):
        search_web(f"OpenAI mental health official {index}", num_results=2)

    diagnostics = sdk_search_diagnostics_from_telemetry(consume_sdk_search_telemetry())

    exa_calls = [call for call in calls if call.startswith("exa:")]
    assert len(exa_calls) == 2
    assert diagnostics["provider_usage"]["exa"]["requests_attempted"] == 2
    assert diagnostics["provider_usage"]["exa"]["requests_succeeded"] == 2
    assert any(
        error.get("provider") == "exa" and error.get("error_type") == "ProviderRequestCapExceeded"
        for error in diagnostics["search_provider_errors"]
    )


def test_search_web_records_sdk_provider_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProvider:
        def __init__(self, provider_name: str) -> None:
            self.provider_name = provider_name

        def search_structured(self, request):
            return [
                SearchResult(
                    title=f"{self.provider_name} result",
                    link=f"https://example.com/{self.provider_name}",
                    snippet="Source-backed search result.",
                    source=self.provider_name,
                )
            ]

    def fake_build_search_provider(provider=None, *, live=False, **_kwargs):
        assert live is True
        return FakeProvider(str(provider))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_PARALLEL", "true")
    monkeypatch.setenv("KEYSTONE_EXA_SEARCH_FALLBACK", "true")
    monkeypatch.setenv("KEYSTONE_TAVILY_SEARCH_FALLBACK", "true")
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.build_search_provider",
        fake_build_search_provider,
    )

    reset_sdk_search_telemetry()
    search_web("Curebase", num_results=2)
    diagnostics = sdk_search_diagnostics_from_telemetry(consume_sdk_search_telemetry())

    assert diagnostics["search_provider_sequence"] == ["searxng", "agents-web-search"]
    assert diagnostics["search_deepening_provider_sequence"] == ["exa", "tavily"]
    assert diagnostics["primary_lane_statuses"]["searxng"] == "used"
    assert diagnostics["primary_lane_statuses"]["agents-web-search"] == "used"
    assert diagnostics["primary_lane_statuses"]["exa"] == "used"
    assert diagnostics["primary_lane_statuses"]["tavily"] == "used"
    assert diagnostics["provider_usage"]["exa"]["raw_result_count"] == 1
    assert diagnostics["provider_result_samples"]["exa"] == [
        {
            "title": "exa result",
            "url": "https://example.com/exa",
            "snippet": "Source-backed search result.",
        }
    ]


def test_search_web_blocks_serper_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setenv("SEARCH_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.delenv("KEYSTONE_SERPER_ENABLED", raising=False)

    with pytest.raises(SerperConfigurationError, match="Serper search is disabled"):
        search_web("Curebase", num_results=1)


def test_search_web_uses_live_serper_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setenv("SEARCH_PROVIDER", "serper")
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.setenv("KEYSTONE_SERPER_ENABLED", "true")

    def fail_get(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("explicit Serper configuration should not call SearXNG first")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.get", fail_get)
    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.requests.post",
        lambda *_args, **_kwargs: MockResponse(),
    )

    assert search_web("Curebase", num_results=1) == [
        SearchResult(
            title="Curebase clinical trial software",
            link="https://example.com/curebase",
            snippet="Curebase supports decentralized clinical trial workflows.",
            source="serper",
        )
    ]


def test_serper_http_error_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorResponse:
        status_code = 403

        def json(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.requests.post",
        lambda *_args, **_kwargs: ErrorResponse(),
    )

    with pytest.raises(SerperSearchError, match="HTTP 403"):
        SerperTool(live=True, api_key="test-key").search("Curebase", num_results=1)


def test_serper_legacy_live_response_includes_source_metadata() -> None:
    payload = SerperTool(live=True, api_key="test-key").search(
        "Curebase",
        max_results=1,
        http_post=lambda *_args, **_kwargs: MockResponse(),
    )

    assert payload == {
        "status": "live",
        "query": "Curebase",
        "results": [
            {
                "title": "Curebase clinical trial software",
                "link": "https://example.com/curebase",
                "url": "https://example.com/curebase",
                "snippet": "Curebase supports decentralized clinical trial workflows.",
                "source": "serper",
                "source_type": "google_search",
            }
        ],
    }


def test_serper_empty_query_and_invalid_result_count_do_not_call_network() -> None:
    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("empty query should return before HTTP")

    assert SerperTool(live=True, api_key="test-key").search(
        "   ",
        num_results=1,
        http_post=fail_post,
    ) == {"status": "live", "query": "   ", "results": []}
    with pytest.raises(ValueError, match="at least 1"):
        SerperTool(live=True, api_key="test-key").search("Curebase", num_results=0)


def test_serper_timeout_request_error_and_invalid_json_are_wrapped() -> None:
    def timeout_post(*_args: object, **_kwargs: object) -> None:
        raise requests.Timeout("slow")

    def request_error_post(*_args: object, **_kwargs: object) -> None:
        raise requests.RequestException("broken")

    class InvalidJsonResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            raise ValueError("not json")

    with pytest.raises(SerperSearchError, match="timed out"):
        SerperTool(live=True, api_key="test-key").search("Curebase", http_post=timeout_post)
    with pytest.raises(SerperSearchError, match="request failed"):
        SerperTool(live=True, api_key="test-key").search(
            "Curebase",
            http_post=request_error_post,
        )
    with pytest.raises(SerperSearchError, match="invalid JSON"):
        SerperTool(live=True, api_key="test-key").search(
            "Curebase",
            http_post=lambda *_args, **_kwargs: InvalidJsonResponse(),
        )


def test_serper_raise_for_status_branch_and_malformed_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RaiseForStatusResponse:
        status_code = None

        def raise_for_status(self) -> None:
            raise requests.RequestException("bad status")

    class MixedResultsResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "organic": [
                    "not-a-dict",
                    {"title": "", "link": "https://example.com/missing-title"},
                    {"title": "Missing link", "link": ""},
                    {
                        "title": "Valid result",
                        "link": "https://example.com/valid",
                        "snippet": "Valid snippet.",
                    },
                ]
            }

    with pytest.raises(SerperSearchError, match="Serper search failed"):
        SerperTool(live=True, api_key="test-key").search(
            "Curebase",
            http_post=lambda *_args, **_kwargs: RaiseForStatusResponse(),
        )

    monkeypatch.setattr(
        "keystone_agents.tools.serper_tool.requests.post",
        lambda *_args, **_kwargs: MixedResultsResponse(),
    )

    assert SerperTool(live=True, api_key="test-key").search("Curebase", num_results=3) == [
        SearchResult(
            title="Valid result",
            link="https://example.com/valid",
            snippet="Valid snippet.",
            source="serper",
        )
    ]
