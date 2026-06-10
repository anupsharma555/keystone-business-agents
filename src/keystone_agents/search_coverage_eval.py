"""Provider-neutral search coverage evaluation for Keystone research agents."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from keystone_agents.source_registry import (
    assess_source_coverage,
    classify_source_lanes,
    normalize_domain,
    result_mapping,
    result_url,
)
from keystone_agents.tools.search_provider import (
    AgentsWebSearchOutput,
    AgentsWebSearchProvider,
    AgentsWebSearchResult,
    SearchRequest,
    build_search_provider,
)

SearchCoverageMode = Literal["wide", "focused", "specific"]
SearchCoverageProviderName = Literal[
    "dry-run",
    "searxng",
    "serper",
    "firecrawl",
    "exa",
    "tavily",
    "brave",
    "browserless",
    "agents-web-search",
]

DEFAULT_SEARCH_COVERAGE_CASES_PATH = Path("evals/provider/search_coverage_cases.jsonl")
DEFAULT_SEARCH_COVERAGE_OUTPUT_DIR = Path("artifacts/search_coverage_evals")
CURRENT_SEARCH_PROVIDERS = frozenset({"dry-run", "searxng", "serper", "firecrawl", "exa", "tavily"})
EVAL_ONLY_SEARCH_PROVIDERS = frozenset({"agents-web-search"})
FUTURE_SEARCH_PROVIDERS = frozenset({"brave", "browserless"})
PAID_OR_METERED_PROVIDERS = frozenset(
    {
        "serper",
        "firecrawl",
        "exa",
        "tavily",
        "brave",
        "browserless",
        "agents-web-search",
    }
)


class SearchCoverageCase(BaseModel):
    """One query-level search coverage eval case."""

    id: str = Field(min_length=1)
    mode: SearchCoverageMode
    query: str = Field(min_length=1)
    category: str = Field(min_length=1)
    expected_source_lanes: list[str] = Field(default_factory=list)
    expected_domains: list[str] = Field(default_factory=list)
    forbidden_domains: list[str] = Field(default_factory=list)
    expected_signals: list[str] = Field(default_factory=list)
    requires_extraction: bool = False
    max_results: int = Field(default=5, ge=1, le=20)
    time_range: str | None = None
    source: str = "web"
    notes: str = ""


class SearchCoverageScore(BaseModel):
    """Agent-useful score for one provider/query run."""

    provider: str
    status: str
    result_count: int = 0
    unique_domain_count: int = 0
    expected_lane_recall: float = 0.0
    expected_lanes_found: list[str] = Field(default_factory=list)
    missing_expected_lanes: list[str] = Field(default_factory=list)
    expected_domain_recall: float = 0.0
    expected_domains_found: list[str] = Field(default_factory=list)
    missing_expected_domains: list[str] = Field(default_factory=list)
    forbidden_domain_hits: list[str] = Field(default_factory=list)
    primary_source_count: int = 0
    useful_unique_domain_count: int = 0
    useful_claim_count: int = 0
    stale_or_noisy_result_count: int = 0
    latency_ms: int = 0
    provider_calls: int = 0
    estimated_cost_units: int = 0
    error: str | None = None
    diagnosis: list[str] = Field(default_factory=list)


class SearchCoverageRunResult(BaseModel):
    """One provider attempt for one search coverage case."""

    case_id: str
    provider: str
    run_index: int
    query: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    score: SearchCoverageScore


class SearchCoverageCaseResult(BaseModel):
    """All provider attempts for one query-level case."""

    case: SearchCoverageCase
    runs: list[SearchCoverageRunResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class SearchCoverageEvalReport(BaseModel):
    """Top-level search coverage eval report."""

    live: bool
    providers: list[str]
    repeats: int
    case_count: int
    results: list[SearchCoverageCaseResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class SearchCoverageProvider(Protocol):
    """Small adapter surface for query-level search coverage providers."""

    provider_name: str
    dry_run: bool


@dataclass(frozen=True)
class DryRunSearchCoverageProvider:
    """Fixture-safe search provider that never touches the network."""

    provider_name: str = "dry-run"
    dry_run: bool = True

    def search_structured(self, request: SearchRequest) -> list[Any]:
        return []


@dataclass(frozen=True)
class UnsupportedSearchCoverageProvider:
    """Explicit future-provider boundary for eval plumbing."""

    provider_name: str
    dry_run: bool = False

    def search_structured(self, request: SearchRequest) -> list[Any]:
        raise NotImplementedError(
            f"{self.provider_name} search provider is not implemented in this repo yet."
        )


@dataclass(frozen=True)
class SearchCoverageEvalOptions:
    """Runtime options for query-level search coverage evals."""

    providers: tuple[SearchCoverageProviderName, ...]
    live: bool = False
    repeats: int = 1


def load_search_coverage_cases(path: str | Path) -> list[SearchCoverageCase]:
    """Load search coverage eval cases from JSONL."""

    case_path = Path(path)
    cases: list[SearchCoverageCase] = []
    for line_number, raw_line in enumerate(case_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {case_path}:{line_number}: {exc}") from exc
        cases.append(SearchCoverageCase.model_validate(payload))
    if not cases:
        raise ValueError(f"No search coverage cases found in {case_path}.")
    return cases


def build_search_coverage_provider(
    provider: SearchCoverageProviderName,
    *,
    live: bool,
) -> SearchCoverageProvider:
    """Build one search provider for eval without executing it."""

    if not live:
        return DryRunSearchCoverageProvider(provider_name=provider)
    if provider == "dry-run":
        return DryRunSearchCoverageProvider()
    if provider in CURRENT_SEARCH_PROVIDERS:
        return build_search_provider(provider=provider, live=True)
    if provider == "agents-web-search":
        return AgentsWebSearchProvider(live=True)
    return UnsupportedSearchCoverageProvider(provider_name=provider)


def run_search_coverage_eval(
    cases: Sequence[SearchCoverageCase],
    *,
    options: SearchCoverageEvalOptions,
    provider_factory: Any = build_search_coverage_provider,
) -> SearchCoverageEvalReport:
    """Compare search providers against query-level coverage cases."""

    providers = normalize_search_coverage_providers(options.providers)
    provider_instances = {
        provider: provider_factory(provider, live=options.live) for provider in providers
    }
    case_results = [
        _run_search_coverage_case(
            case,
            providers=providers,
            provider_instances=provider_instances,
            repeats=options.repeats,
        )
        for case in cases
    ]
    return SearchCoverageEvalReport(
        live=options.live,
        providers=list(providers),
        repeats=options.repeats,
        case_count=len(case_results),
        results=case_results,
        summary=_summarize_eval(case_results),
    )


def write_search_coverage_artifacts(
    report: SearchCoverageEvalReport,
    output_dir: str | Path,
) -> dict[str, str]:
    """Write summary and raw JSON artifacts for a search coverage eval report."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    raw_dir = root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    summary_path = root / "summary.json"
    results_path = root / "results.jsonl"
    summary_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    with results_path.open("w", encoding="utf-8") as handle:
        for case_result in report.results:
            handle.write(json.dumps(case_result.model_dump(mode="json"), ensure_ascii=True))
            handle.write("\n")
            for run in case_result.runs:
                raw_name = f"{case_result.case.id}__{run.provider}__run{run.run_index}.json"
                raw_path = raw_dir / raw_name
                raw_path.write_text(
                    json.dumps(run.model_dump(mode="json"), ensure_ascii=True, indent=2),
                    encoding="utf-8",
                )
    return {"summary": str(summary_path), "results": str(results_path), "raw_dir": str(raw_dir)}


def render_search_coverage_eval_report(report: SearchCoverageEvalReport) -> str:
    """Render a concise Markdown report."""

    lines = [
        "# Search Coverage Eval",
        "",
        f"- Mode: {'live' if report.live else 'dry-run'}",
        f"- Providers: {', '.join(report.providers)}",
        f"- Repeats: {report.repeats}",
        f"- Cases: {report.case_count}",
        "",
        "## Provider Summary",
    ]
    for provider, metrics in sorted((report.summary.get("providers") or {}).items()):
        lines.append(
            "- "
            f"{provider}: lane recall {metrics.get('average_expected_lane_recall', 0):.2f}, "
            f"domain recall {metrics.get('average_expected_domain_recall', 0):.2f}, "
            f"primary {metrics.get('average_primary_source_count', 0):.1f}, "
            f"domains {metrics.get('average_unique_domain_count', 0):.1f}, "
            f"p50/p95 {metrics.get('latency_p50_ms', 0)}/"
            f"{metrics.get('latency_p95_ms', 0)} ms, "
            f"errors {metrics.get('error_rate', 0):.2f}"
        )
    lines.extend(["", "## Cases"])
    for case_result in report.results:
        best = case_result.summary.get("best_provider_by_lane_recall") or ""
        lines.append(
            f"- {case_result.case.id} ({case_result.case.mode}, "
            f"{case_result.case.category}): best lane recall {best or 'n/a'}"
        )
    return "\n".join(lines)


def normalize_search_coverage_providers(
    values: Sequence[str] | None,
) -> tuple[SearchCoverageProviderName, ...]:
    """Normalize CLI provider names."""

    raw_values = list(values or ["searxng", "serper", "firecrawl"])
    providers: list[SearchCoverageProviderName] = []
    valid = set(CURRENT_SEARCH_PROVIDERS | EVAL_ONLY_SEARCH_PROVIDERS | FUTURE_SEARCH_PROVIDERS)
    for value in raw_values:
        normalized = str(value or "").strip().lower().replace("_", "-")
        aliases = {
            "dryrun": "dry-run",
            "dry_run": "dry-run",
            "brave-search": "brave",
            "browserless-search": "browserless",
            "openai-web-search": "agents-web-search",
            "openai-websearch": "agents-web-search",
            "agents-websearch": "agents-web-search",
            "sdk-web-search": "agents-web-search",
            "native-web-search": "agents-web-search",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in valid:
            raise ValueError(
                "provider must be dry-run, searxng, serper, firecrawl, exa, "
                "tavily, brave, browserless, or agents-web-search"
            )
        providers.append(normalized)  # type: ignore[arg-type]
    return tuple(dict.fromkeys(providers))


def score_search_results(
    case: SearchCoverageCase,
    *,
    provider: str,
    results: Sequence[Any],
    latency_ms: int,
    status: str = "success",
    error: str | None = None,
) -> SearchCoverageScore:
    """Score one provider result packet for agent-useful coverage."""

    normalized_results = [_jsonable_result(result) for result in results]
    coverage = assess_source_coverage(
        normalized_results,
        expected_lanes=case.expected_source_lanes,
        expected_domains=case.expected_domains,
        forbidden_domains=case.forbidden_domains,
    )
    expected_domains_found = [
        domain
        for domain in coverage.expected_domains
        if any(
            observed == domain or observed.endswith(f".{domain}")
            for observed in coverage.observed_domains
        )
    ]
    signal_hits = _signal_hits(normalized_results, case.expected_signals)
    diagnosis = list(coverage.diagnosis)
    if (
        case.requires_extraction
        and normalized_results
        and not any(str(result.get("content") or "").strip() for result in normalized_results)
    ):
        diagnosis.append("selected URL extraction required but provider returned snippets only")
    if error:
        diagnosis.append(error)
    return SearchCoverageScore(
        provider=provider,
        status=status,
        result_count=len(normalized_results),
        unique_domain_count=len(coverage.observed_domains),
        expected_lane_recall=coverage.expected_lane_recall,
        expected_lanes_found=list(coverage.observed_lanes),
        missing_expected_lanes=list(coverage.missing_lanes),
        expected_domain_recall=coverage.expected_domain_recall,
        expected_domains_found=expected_domains_found,
        missing_expected_domains=list(coverage.missing_expected_domains),
        forbidden_domain_hits=list(coverage.forbidden_domain_hits),
        primary_source_count=coverage.primary_source_count,
        useful_unique_domain_count=coverage.useful_unique_domain_count,
        useful_claim_count=len(signal_hits),
        stale_or_noisy_result_count=_stale_or_noisy_count(
            normalized_results,
            forbidden_domains=coverage.forbidden_domains,
        ),
        latency_ms=latency_ms,
        provider_calls=1,
        estimated_cost_units=1 if provider in PAID_OR_METERED_PROVIDERS else 0,
        error=error,
        diagnosis=list(dict.fromkeys(diagnosis)),
    )


def _run_search_coverage_case(
    case: SearchCoverageCase,
    *,
    providers: Sequence[SearchCoverageProviderName],
    provider_instances: Mapping[str, SearchCoverageProvider],
    repeats: int,
) -> SearchCoverageCaseResult:
    runs: list[SearchCoverageRunResult] = []
    for run_index in range(1, max(1, repeats) + 1):
        for provider in providers:
            started_at = perf_counter()
            results: list[Any] = []
            status = "success"
            error = None
            try:
                results = _search_with_provider(provider_instances[provider], case)
                if getattr(provider_instances[provider], "dry_run", False):
                    status = "dry-run"
            except Exception as exc:
                status = "error"
                error = str(exc)
            score = score_search_results(
                case,
                provider=provider,
                results=results,
                latency_ms=_elapsed_ms(started_at),
                status=status,
                error=error,
            )
            score = score.model_copy(
                update={
                    "estimated_cost_units": _provider_estimated_cost_units(
                        provider_instances[provider],
                        provider,
                        default=score.estimated_cost_units,
                    )
                }
            )
            runs.append(
                SearchCoverageRunResult(
                    case_id=case.id,
                    provider=provider,
                    run_index=run_index,
                    query=case.query,
                    results=[_jsonable_result(result) for result in results],
                    score=score,
                )
            )
    return SearchCoverageCaseResult(case=case, runs=runs, summary=_summarize_case(runs))


def _provider_estimated_cost_units(
    provider_instance: SearchCoverageProvider,
    provider_name: str,
    *,
    default: int,
) -> int:
    usage = getattr(provider_instance, "last_credit_usage", None)
    if isinstance(usage, dict):
        try:
            credits = int(float(usage.get("request_credits") or 0))
        except (TypeError, ValueError):
            credits = 0
        if credits > 0:
            return credits
    return default


def _search_with_provider(provider: SearchCoverageProvider, case: SearchCoverageCase) -> list[Any]:
    request = SearchRequest(
        query=case.query,
        num_results=case.max_results,
        source=case.source,
        time_range=case.time_range,
        safe_search=1,
    )
    structured = getattr(provider, "search_structured", None)
    if callable(structured):
        return list(structured(request))
    search_web = getattr(provider, "search_web", None)
    if callable(search_web):
        return list(search_web(case.query, num_results=case.max_results))
    search = getattr(provider, "search", None)
    if callable(search):
        return list(search(case.query, num_results=case.max_results))
    raise TypeError(f"{type(provider).__name__} does not expose a search method")


def _summarize_case(runs: Sequence[SearchCoverageRunResult]) -> dict[str, Any]:
    best_provider = ""
    best_recall = -1.0
    for run in runs:
        if run.score.expected_lane_recall > best_recall:
            best_recall = run.score.expected_lane_recall
            best_provider = run.provider
    return {
        "best_provider_by_lane_recall": best_provider,
        "providers": _summarize_provider_runs(runs),
    }


def _summarize_eval(case_results: Sequence[SearchCoverageCaseResult]) -> dict[str, Any]:
    runs = [run for case_result in case_results for run in case_result.runs]
    modes: dict[str, int] = defaultdict(int)
    for case_result in case_results:
        modes[case_result.case.mode] += 1
    return {"case_modes": dict(modes), "providers": _summarize_provider_runs(runs)}


def _summarize_provider_runs(
    runs: Sequence[SearchCoverageRunResult],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[SearchCoverageRunResult]] = defaultdict(list)
    for run in runs:
        grouped[run.provider].append(run)
    summary: dict[str, dict[str, Any]] = {}
    for provider, provider_runs in grouped.items():
        scores = [run.score for run in provider_runs]
        latencies = [score.latency_ms for score in scores]
        error_count = sum(1 for score in scores if score.status == "error" or score.error)
        summary[provider] = {
            "run_count": len(provider_runs),
            "error_count": error_count,
            "error_rate": _rate(error_count, len(provider_runs)),
            "average_expected_lane_recall": _average(
                score.expected_lane_recall for score in scores
            ),
            "average_expected_domain_recall": _average(
                score.expected_domain_recall for score in scores
            ),
            "average_primary_source_count": _average(
                score.primary_source_count for score in scores
            ),
            "average_unique_domain_count": _average(score.unique_domain_count for score in scores),
            "average_useful_claim_count": _average(score.useful_claim_count for score in scores),
            "estimated_cost_units": sum(score.estimated_cost_units for score in scores),
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "repeatability": _repeatability(provider_runs),
        }
    return summary


def _jsonable_result(result: Any) -> dict[str, Any]:
    mapping = result_mapping(result)
    url = result_url(mapping)
    title = str(mapping.get("title") or "")
    snippet = str(mapping.get("snippet") or mapping.get("description") or "")
    content = str(mapping.get("content") or mapping.get("markdown") or "")
    source_type = str(mapping.get("source_type") or mapping.get("source") or "")
    lanes = classify_source_lanes(
        url=url,
        title=title,
        snippet=" ".join([snippet, content[:500]]),
        source_type=source_type,
    )
    payload = {
        "title": title,
        "url": url,
        "snippet": snippet,
        "content": content,
        "source": str(mapping.get("source") or ""),
        "source_type": source_type,
        "date": mapping.get("date") or mapping.get("published_at"),
        "source_lanes": list(lanes),
    }
    return {key: value for key, value in payload.items() if value not in ("", None, [])}


def _signal_hits(results: Sequence[Mapping[str, Any]], signals: Sequence[str]) -> list[str]:
    text = " ".join(
        " ".join(str(result.get(key) or "") for key in ("title", "snippet", "content"))
        for result in results
    ).lower()
    hits: list[str] = []
    for signal in signals:
        normalized = str(signal or "").strip().lower()
        if normalized and normalized in text:
            hits.append(str(signal))
    return list(dict.fromkeys(hits))


def _stale_or_noisy_count(
    results: Sequence[Mapping[str, Any]],
    *,
    forbidden_domains: Sequence[str],
) -> int:
    count = 0
    for result in results:
        domain = normalize_domain(str(result.get("url") or ""))
        date = str(result.get("date") or "")
        if any(domain == item or domain.endswith(f".{item}") for item in forbidden_domains):
            count += 1
            continue
        if _is_stale_result_date(date):
            count += 1
    return count


def _is_stale_result_date(date: str) -> bool:
    normalized = str(date or "").lower()
    if not normalized:
        return False
    current_year = datetime.now(UTC).year
    for year in re.findall(r"\b(20\d{2})\b", normalized):
        if int(year) <= current_year - 3:
            return True
    year_ago = re.search(r"\b(\d+(?:\.\d+)?)\s+years?\s+ago\b", normalized)
    if year_ago and float(year_ago.group(1)) >= 3:
        return True
    month_ago = re.search(r"\b(\d+)\s+months?\s+ago\b", normalized)
    return bool(month_ago and int(month_ago.group(1)) >= 36)


def _repeatability(runs: Sequence[SearchCoverageRunResult]) -> dict[str, Any]:
    if len(runs) <= 1:
        return {"runs": len(runs), "stable": True}
    statuses = {run.score.status for run in runs}
    lane_recalls = [run.score.expected_lane_recall for run in runs]
    domain_recalls = [run.score.expected_domain_recall for run in runs]
    stable = (
        len(statuses) == 1
        and (max(lane_recalls) - min(lane_recalls) <= 0.1)
        and (max(domain_recalls) - min(domain_recalls) <= 0.1)
    )
    return {
        "runs": len(runs),
        "stable": stable,
        "status_count": len(statuses),
        "lane_recall_range": round(max(lane_recalls) - min(lane_recalls), 3),
        "domain_recall_range": round(max(domain_recalls) - min(domain_recalls), 3),
    }


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def _rate(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


def _average(values: Iterable[float | int]) -> float:
    items = [float(value) for value in values]
    return round(sum(items) / len(items), 3) if items else 0.0


def _percentile(values: Sequence[int], percentile: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    if percentile == 50:
        return round(median(ordered))
    index = min(len(ordered) - 1, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[index]


__all__ = [
    "AgentsWebSearchOutput",
    "AgentsWebSearchProvider",
    "AgentsWebSearchResult",
    "CURRENT_SEARCH_PROVIDERS",
    "DEFAULT_SEARCH_COVERAGE_CASES_PATH",
    "DEFAULT_SEARCH_COVERAGE_OUTPUT_DIR",
    "EVAL_ONLY_SEARCH_PROVIDERS",
    "FUTURE_SEARCH_PROVIDERS",
    "SearchCoverageCase",
    "SearchCoverageCaseResult",
    "SearchCoverageEvalOptions",
    "SearchCoverageEvalReport",
    "SearchCoverageRunResult",
    "SearchCoverageScore",
    "UnsupportedSearchCoverageProvider",
    "build_search_coverage_provider",
    "load_search_coverage_cases",
    "normalize_search_coverage_providers",
    "render_search_coverage_eval_report",
    "run_search_coverage_eval",
    "score_search_results",
    "write_search_coverage_artifacts",
]
