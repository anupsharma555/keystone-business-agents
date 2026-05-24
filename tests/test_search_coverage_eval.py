from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from keystone_agents.search_coverage_eval import (
    AgentsWebSearchOutput,
    AgentsWebSearchProvider,
    AgentsWebSearchResult,
    SearchCoverageCase,
    SearchCoverageEvalOptions,
    build_search_coverage_provider,
    load_search_coverage_cases,
    normalize_search_coverage_providers,
    run_search_coverage_eval,
    score_search_results,
    write_search_coverage_artifacts,
)
from keystone_agents.tools.search_provider import SearchRequest, SearchResult


def test_load_search_coverage_cases_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "wide-query",
                "mode": "wide",
                "query": "behavioral health AI grants",
                "category": "grant_discovery",
                "expected_source_lanes": ["grants_funding"],
                "expected_domains": ["reporter.nih.gov"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_search_coverage_cases(path)

    assert len(cases) == 1
    assert cases[0].id == "wide-query"
    assert cases[0].expected_source_lanes == ["grants_funding"]


def test_score_search_results_measures_lane_domain_and_claim_coverage() -> None:
    case = SearchCoverageCase(
        id="grant",
        mode="wide",
        query="behavioral health AI grant",
        category="grant_discovery",
        expected_source_lanes=["grants_funding", "clinical_trials"],
        expected_domains=["reporter.nih.gov"],
        forbidden_domains=["wikipedia.org"],
        expected_signals=["SBIR", "behavioral health"],
    )

    score = score_search_results(
        case,
        provider="serper",
        results=[
            SearchResult(
                title="NIH SBIR grant for behavioral health AI",
                link="https://reporter.nih.gov/project-details/123",
                snippet="SBIR grant supports behavioral health AI.",
                source="serper",
            )
        ],
        latency_ms=42,
    )

    assert score.expected_lane_recall == 0.5
    assert score.expected_domain_recall == 1.0
    assert score.primary_source_count == 1
    assert score.useful_claim_count == 2
    assert score.missing_expected_lanes == ["clinical_trials"]


def test_score_search_results_counts_relative_stale_dates() -> None:
    case = SearchCoverageCase(
        id="trial",
        mode="specific",
        query="site:clinicaltrials.gov depression AI",
        category="clinical_trial_search",
        expected_source_lanes=["clinical_trials"],
        expected_domains=["clinicaltrials.gov"],
    )

    score = score_search_results(
        case,
        provider="agents-web-search",
        results=[
            SearchResult(
                title="Older depression AI study",
                link="https://clinicaltrials.gov/study/NCT01234567",
                snippet="Depression artificial intelligence clinical trial.",
                source="agents-web-search",
                date="6.7 years ago",
            ),
            SearchResult(
                title="Recent depression AI study",
                link="https://clinicaltrials.gov/study/NCT07654321",
                snippet="Depression artificial intelligence clinical trial.",
                source="agents-web-search",
                date="last month",
            ),
        ],
        latency_ms=42,
    )

    assert score.stale_or_noisy_result_count == 1


def test_run_search_coverage_eval_compares_current_and_future_provider_boundaries() -> None:
    case = SearchCoverageCase(
        id="specific-trial",
        mode="specific",
        query="site:clinicaltrials.gov depression AI",
        category="clinical_trial_search",
        expected_source_lanes=["clinical_trials"],
        expected_domains=["clinicaltrials.gov"],
        expected_signals=["depression"],
    )

    @dataclass(frozen=True)
    class FakeProvider:
        provider_name: str
        dry_run: bool = False

        def search_structured(self, request: SearchRequest) -> list[SearchResult]:
            if self.provider_name == "exa":
                return [
                    SearchResult(
                        title="Depression AI Study - ClinicalTrials.gov",
                        link="https://clinicaltrials.gov/study/NCT01234567",
                        snippet="Depression artificial intelligence clinical trial.",
                        source=self.provider_name,
                    )
                ][: request.num_results]
            return [
                SearchResult(
                    title="Generic article",
                    link="https://example.com/article",
                    snippet="Search result without trial coverage.",
                    source=self.provider_name,
                )
            ][: request.num_results]

    def fake_factory(provider, **_kwargs):
        return FakeProvider(provider_name=provider)

    report = run_search_coverage_eval(
        [case],
        options=SearchCoverageEvalOptions(providers=("searxng", "exa"), live=True),
        provider_factory=fake_factory,
    )

    scores = {run.provider: run.score for run in report.results[0].runs}
    assert report.providers == ["searxng", "exa"]
    assert scores["exa"].expected_lane_recall == 1.0
    assert scores["searxng"].expected_lane_recall == 0.0
    assert report.results[0].summary["best_provider_by_lane_recall"] == "exa"


def test_search_coverage_provider_normalizer_accepts_future_boundaries() -> None:
    assert normalize_search_coverage_providers(
        [
            "dry-run",
            "searxng",
            "serper",
            "firecrawl",
            "exa",
            "tavily",
            "brave",
            "browserless",
            "agents-web-search",
            "openai-web-search",
        ]
    ) == (
        "dry-run",
        "searxng",
        "serper",
        "firecrawl",
        "exa",
        "tavily",
        "brave",
        "browserless",
        "agents-web-search",
    )


def test_agents_web_search_provider_is_eval_only_and_dry_run_safe() -> None:
    dry_provider = build_search_coverage_provider("agents-web-search", live=False)
    live_provider = build_search_coverage_provider("agents-web-search", live=True)

    assert dry_provider.dry_run is True
    assert dry_provider.provider_name == "agents-web-search"
    assert isinstance(live_provider, AgentsWebSearchProvider)
    assert live_provider.dry_run is False


def test_agents_web_search_provider_normalizes_structured_output() -> None:
    request = SearchRequest(query="behavioral health grants", num_results=3)

    provider = AgentsWebSearchProvider(
        live=True,
        runner=lambda _prompt, _request: AgentsWebSearchOutput(
            results=[
                AgentsWebSearchResult(
                    title="NIH RePORTER project",
                    url="https://reporter.nih.gov/project-details/123",
                    snippet="Behavioral health AI grant.",
                    date="2026-01-15",
                )
            ]
        ),
    )

    results = provider.search_structured(request)

    assert results == [
        SearchResult(
            title="NIH RePORTER project",
            link="https://reporter.nih.gov/project-details/123",
            snippet="Behavioral health AI grant.",
            source="agents-web-search",
            date="2026-01-15",
        )
    ]


def test_write_search_coverage_artifacts(tmp_path: Path) -> None:
    case = SearchCoverageCase(
        id="dry",
        mode="focused",
        query="example company",
        category="company_research",
    )
    report = run_search_coverage_eval(
        [case],
        options=SearchCoverageEvalOptions(providers=("searxng",), live=False),
    )

    paths = write_search_coverage_artifacts(report, tmp_path)

    assert Path(paths["summary"]).is_file()
    assert Path(paths["results"]).is_file()
    assert Path(paths["raw_dir"]).is_dir()
