"""Provider-neutral browser extraction evaluation for Keystone research agents."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Literal, Protocol
from urllib.parse import urljoin, urlparse

import requests
from pydantic import BaseModel, Field

from keystone_agents.source_enrichment import extract_clean_text
from keystone_agents.tools.browserless_tool import fetch_rendered_page
from keystone_agents.tools.playwright_tool import render_page_impl
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    extract_website_content,
    extract_website_content_from_html,
)

BrowserEvalMode = Literal["wide", "focused", "specific"]
BrowserProviderName = Literal[
    "trafilatura",
    "firecrawl",
    "browserless",
    "apify",
    "playwright",
    "crawl4ai",
]

DEFAULT_BROWSER_EVAL_CASES_PATH = Path("evals/provider/browser_extraction_cases.jsonl")
DEFAULT_BROWSER_EVAL_OUTPUT_DIR = Path("artifacts/browser_extraction_evals")
BASELINE_BROWSER_PROVIDER: BrowserProviderName = "trafilatura"
DEFAULT_RENDERED_PAGE_TIMEOUT_SECONDS = 15
DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS = 500_000
DEFAULT_FORBIDDEN_SIGNALS = ("captcha", "access denied", "cloudflare", "enable javascript")
BOILERPLATE_TERMS = (
    "privacy policy",
    "terms of use",
    "cookie",
    "subscribe",
    "newsletter",
    "sign in",
    "log in",
    "all rights reserved",
)
INTERNAL_LINK_HINTS = (
    "about",
    "leadership",
    "team",
    "news",
    "press",
    "careers",
    "jobs",
    "partners",
    "research",
    "grants",
    "trial",
    "events",
    "conference",
    "contact",
)


class RenderedLink(BaseModel):
    """Normalized link extracted from rendered or static page content."""

    url: str = ""
    text: str = ""
    internal: bool = False


class RenderedPage(BaseModel):
    """Provider-neutral rendered page contract."""

    provider: str
    url: str
    final_url: str = ""
    status: str = "error"
    title: str = ""
    text_or_markdown: str = ""
    links: list[RenderedLink] = Field(default_factory=list)
    html_length: int = 0
    latency_ms: int = 0
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BrowserExtractionCase(BaseModel):
    """One URL-level extraction eval case."""

    id: str = Field(min_length=1)
    mode: BrowserEvalMode
    url: str = Field(min_length=1)
    category: str = Field(min_length=1)
    difficulty_tags: list[str] = Field(default_factory=list)
    expected_signals: list[str] = Field(default_factory=list)
    forbidden_signals: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=DEFAULT_RENDERED_PAGE_TIMEOUT_SECONDS, ge=1)
    notes: str = ""


class BrowserExtractionScore(BaseModel):
    """Agent-useful deterministic score for one provider run."""

    provider: str
    status: str
    extraction_success: bool
    expected_signal_recall: float
    expected_signal_hits: list[str] = Field(default_factory=list)
    forbidden_signal_hits: list[str] = Field(default_factory=list)
    access_blocked: bool = False
    title_present: bool = False
    useful_text_length: int = 0
    boilerplate_ratio: float = 0.0
    useful_internal_link_count: int = 0
    latency_ms: int = 0
    error: str | None = None
    quality_bucket: str = "unreadable"
    quality_diagnosis: list[str] = Field(default_factory=list)
    improvement_over_baseline: dict[str, Any] = Field(default_factory=dict)


class BrowserExtractionRunResult(BaseModel):
    """One provider attempt for one case."""

    case_id: str
    provider: str
    run_index: int
    page: RenderedPage
    score: BrowserExtractionScore


class BrowserExtractionCaseResult(BaseModel):
    """All provider attempts for one URL-level case."""

    case: BrowserExtractionCase
    runs: list[BrowserExtractionRunResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class BrowserExtractionEvalReport(BaseModel):
    """Top-level browser extraction eval report."""

    live: bool
    providers: list[str]
    baseline_provider: str
    repeats: int
    case_count: int
    results: list[BrowserExtractionCaseResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class BrowserProviderDiagnosticSpec(BaseModel):
    """Human-readable rendered-page provider role and promotion guidance."""

    role: str
    promotion_status: str
    readiness: str
    budget_class: str
    default_use: str
    live_requirement: str
    benchmark_focus: str
    next_validation: str
    promotion_rule: str


class RenderedPageProvider(Protocol):
    """Small adapter surface for browser/static rendered page extraction providers."""

    provider_name: str
    dry_run: bool

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        """Render or extract one URL into the normalized page contract."""


@dataclass(frozen=True)
class DryRunRenderedPageProvider:
    """Fixture-safe provider that never touches the network."""

    provider_name: str
    dry_run: bool = True

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        return RenderedPage(
            provider=self.provider_name,
            url=url,
            final_url=url,
            status="dry-run",
            latency_ms=0,
            metadata={"timeout_seconds": timeout_seconds},
        )


@dataclass(frozen=True)
class TrafilaturaRenderedPageProvider:
    """Static HTTP + Trafilatura baseline extraction provider."""

    dry_run: bool = False
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS
    provider_name: str = BASELINE_BROWSER_PROVIDER

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        if self.dry_run:
            return DryRunRenderedPageProvider(self.provider_name).render(url, timeout_seconds)
        started_at = perf_counter()
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 KeystoneBusinessAgents/0.1 "
                        "(browser extraction eval; contact: operator)"
                    )
                },
                timeout=timeout_seconds,
            )
            status_code = int(getattr(response, "status_code", 0) or 0)
            response.raise_for_status()
        except requests.RequestException as exc:
            return _error_page(
                provider=self.provider_name,
                url=url,
                error=f"Trafilatura request failed: {exc}",
                latency_ms=_elapsed_ms(started_at),
            )
        html = str(getattr(response, "text", "") or "")
        truncated_html = html[: self.max_output_chars]
        try:
            extraction = extract_website_content_from_html(
                truncated_html,
                url=str(getattr(response, "url", "") or url),
                company_name=urlparse(url).netloc or "web page",
            )
        except WebsiteExtractionError as exc:
            return _error_page(
                provider=self.provider_name,
                url=url,
                error=str(exc),
                latency_ms=_elapsed_ms(started_at),
            )
        return RenderedPage(
            provider=self.provider_name,
            url=url,
            final_url=str(getattr(response, "url", "") or url),
            status=extraction.status,
            title=extraction.title,
            text_or_markdown=_truncate(extraction.text_or_markdown, self.max_output_chars),
            links=_extract_links(truncated_html, base_url=str(getattr(response, "url", "") or url)),
            html_length=len(html),
            latency_ms=_elapsed_ms(started_at),
            metadata={
                "status_code": status_code,
                "output_truncated": len(extraction.text_or_markdown) > self.max_output_chars,
            },
        )


@dataclass(frozen=True)
class FirecrawlRenderedPageProvider:
    """Firecrawl scrape provider, optional and credential-gated by existing settings."""

    dry_run: bool = False
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS
    provider_name: str = "firecrawl"

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        if self.dry_run:
            return DryRunRenderedPageProvider(self.provider_name).render(url, timeout_seconds)
        started_at = perf_counter()
        try:
            result = extract_website_content(
                url,
                company_name=urlparse(url).netloc or "web page",
                provider="firecrawl",
                live=True,
            )
        except WebsiteExtractionError as exc:
            return _error_page(
                provider=self.provider_name,
                url=url,
                error=str(exc),
                latency_ms=_elapsed_ms(started_at),
            )
        return RenderedPage(
            provider=self.provider_name,
            url=url,
            final_url=result.url,
            status=result.status,
            title=result.title,
            text_or_markdown=_truncate(result.text_or_markdown, self.max_output_chars),
            links=[],
            html_length=0,
            latency_ms=_elapsed_ms(started_at),
            metadata={
                **result.metadata,
                "output_truncated": len(result.text_or_markdown) > self.max_output_chars,
                "timeout_seconds": timeout_seconds,
            },
        )


@dataclass(frozen=True)
class BrowserlessRenderedPageProvider:
    """Browserless provider boundary. Live Browserless is intentionally not implemented yet."""

    dry_run: bool = False
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS
    provider_name: str = "browserless"

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        if self.dry_run:
            return DryRunRenderedPageProvider(self.provider_name).render(url, timeout_seconds)
        started_at = perf_counter()
        try:
            result = fetch_rendered_page(url, live=True)
        except (RuntimeError, NotImplementedError) as exc:
            return _error_page(
                provider=self.provider_name,
                url=url,
                error=str(exc),
                latency_ms=_elapsed_ms(started_at),
            )
        html = result.html or ""
        return RenderedPage(
            provider=self.provider_name,
            url=url,
            final_url=result.url,
            status=result.status,
            title=result.title or "",
            text_or_markdown=_truncate(result.text_or_markdown, self.max_output_chars),
            links=_extract_links(html, base_url=result.url),
            html_length=len(html),
            latency_ms=_elapsed_ms(started_at),
            metadata={"timeout_seconds": timeout_seconds, "source": result.source},
        )


@dataclass(frozen=True)
class PlaywrightRenderedPageProvider:
    """Optional read-only local Chromium rendering provider."""

    dry_run: bool = False
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS
    provider_name: str = "playwright"

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        if self.dry_run:
            return DryRunRenderedPageProvider(self.provider_name).render(url, timeout_seconds)
        started_at = perf_counter()
        result = render_page_impl(
            url,
            timeout_seconds=timeout_seconds,
            live=True,
            max_text_chars=self.max_output_chars,
        )
        return RenderedPage(
            provider=self.provider_name,
            url=str(result.get("url") or url),
            final_url=str(result.get("final_url") or url),
            status=str(result.get("status") or "error"),
            title=str(result.get("title") or ""),
            text_or_markdown=_truncate(
                str(result.get("text_or_markdown") or ""), self.max_output_chars
            ),
            links=[
                RenderedLink(url=str(link.get("url") or ""), text=str(link.get("text") or ""))
                for link in result.get("links", [])
                if isinstance(link, Mapping)
            ],
            html_length=int(result.get("html_length") or 0),
            latency_ms=int(result.get("latency_ms") or _elapsed_ms(started_at)),
            error=str(result.get("error") or "") or None,
            metadata=dict(result.get("metadata") or {}),
        )


@dataclass(frozen=True)
class UnsupportedRenderedPageProvider:
    """Explicit placeholder for future browser providers without changing eval logic."""

    provider_name: str
    dry_run: bool = False

    def render(self, url: str, timeout_seconds: int) -> RenderedPage:
        return RenderedPage(
            provider=self.provider_name,
            url=url,
            final_url=url,
            status="unsupported",
            latency_ms=0,
            error=f"{self.provider_name} rendered-page provider is not implemented yet.",
            metadata={"timeout_seconds": timeout_seconds},
        )


@dataclass(frozen=True)
class BrowserExtractionEvalOptions:
    """Runtime options for a browser extraction eval run."""

    providers: tuple[BrowserProviderName, ...]
    baseline_provider: BrowserProviderName = BASELINE_BROWSER_PROVIDER
    live: bool = False
    repeats: int = 1
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS


def load_browser_extraction_cases(path: str | Path) -> list[BrowserExtractionCase]:
    """Load browser extraction cases from JSONL."""

    case_path = Path(path)
    cases: list[BrowserExtractionCase] = []
    for line_number, raw_line in enumerate(case_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {case_path}:{line_number}: {exc}") from exc
        cases.append(BrowserExtractionCase.model_validate(payload))
    if not cases:
        raise ValueError(f"No browser extraction cases found in {case_path}.")
    return cases


def build_rendered_page_provider(
    provider: BrowserProviderName,
    *,
    live: bool,
    max_output_chars: int = DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS,
) -> RenderedPageProvider:
    """Build one rendered page provider without executing it."""

    if not live:
        return DryRunRenderedPageProvider(provider_name=provider)
    if provider == "firecrawl":
        return FirecrawlRenderedPageProvider(max_output_chars=max_output_chars)
    if provider == "browserless":
        return BrowserlessRenderedPageProvider(max_output_chars=max_output_chars)
    if provider == "playwright":
        return PlaywrightRenderedPageProvider(max_output_chars=max_output_chars)
    if provider in {"apify", "crawl4ai"}:
        return UnsupportedRenderedPageProvider(provider_name=provider)
    return TrafilaturaRenderedPageProvider(max_output_chars=max_output_chars)


def run_browser_extraction_eval(
    cases: Sequence[BrowserExtractionCase],
    *,
    options: BrowserExtractionEvalOptions,
    provider_factory: Any = build_rendered_page_provider,
) -> BrowserExtractionEvalReport:
    """Compare rendered page providers against URL-level eval cases."""

    providers = _providers_with_baseline(options.providers, options.baseline_provider)
    provider_instances = {
        provider: provider_factory(
            provider,
            live=options.live,
            max_output_chars=options.max_output_chars,
        )
        for provider in providers
    }
    case_results = [
        _run_browser_case(
            case,
            providers=providers,
            provider_instances=provider_instances,
            repeats=options.repeats,
            baseline_provider=options.baseline_provider,
        )
        for case in cases
    ]
    return BrowserExtractionEvalReport(
        live=options.live,
        providers=list(providers),
        baseline_provider=options.baseline_provider,
        repeats=options.repeats,
        case_count=len(case_results),
        results=case_results,
        summary=_summarize_eval(case_results, baseline_provider=options.baseline_provider),
    )


def write_browser_extraction_artifacts(
    report: BrowserExtractionEvalReport,
    output_dir: str | Path,
) -> dict[str, str]:
    """Write summary and raw JSON artifacts for a browser extraction eval report."""

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


def render_browser_extraction_eval_report(report: BrowserExtractionEvalReport) -> str:
    """Render a concise Markdown report."""

    lines = [
        "# Browser Extraction Eval",
        "",
        f"- Mode: {'live' if report.live else 'dry-run'}",
        f"- Providers: {', '.join(report.providers)}",
        f"- Baseline: {report.baseline_provider}",
        f"- Repeats: {report.repeats}",
        f"- Cases: {report.case_count}",
        "",
        "## Provider Summary",
    ]
    for provider, metrics in sorted((report.summary.get("providers") or {}).items()):
        lines.append(
            "- "
            f"{provider}: success {metrics.get('success_rate', 0):.2f}, "
            f"avg recall {metrics.get('average_expected_signal_recall', 0):.2f}, "
            f"text avg {metrics.get('average_useful_text_length', 0)}, "
            f"strong {metrics.get('strong_quality_count', 0)}, "
            f"weak/unreadable {metrics.get('weak_or_unreadable_count', 0)}, "
            f"p50/p95 {metrics.get('latency_p50_ms', 0)}/"
            f"{metrics.get('latency_p95_ms', 0)} ms, "
            f"errors {metrics.get('error_rate', 0):.2f}"
        )
    provider_specs = report.summary.get("provider_specs") or {}
    if provider_specs:
        lines.extend(["", "## Provider Roles"])
        for provider in report.providers:
            spec = provider_specs.get(provider)
            if not isinstance(spec, Mapping):
                continue
            lines.append(
                "- "
                f"{provider}: {spec.get('promotion_status')} - {spec.get('role')} "
                f"({spec.get('budget_class')}; {spec.get('default_use')}; "
                f"readiness: {spec.get('readiness')})"
            )
    lines.extend(["", "## Cases"])
    for case_result in report.results:
        improvement_count = int(case_result.summary.get("providers_improved_over_baseline") or 0)
        weak_count = int(case_result.summary.get("weak_or_unreadable_runs") or 0)
        lines.append(
            f"- {case_result.case.id} ({case_result.case.mode}, {case_result.case.category}): "
            f"{improvement_count} provider run(s) improved over baseline; "
            f"{weak_count} weak/unreadable run(s)"
        )
    return "\n".join(lines)


def normalize_browser_providers(values: Sequence[str] | None) -> tuple[BrowserProviderName, ...]:
    """Normalize CLI provider names."""

    raw_values = list(values or ["browserless"])
    providers: list[BrowserProviderName] = []
    for value in raw_values:
        normalized = str(value or "").strip().lower()
        if normalized in {"local", "static", "trafilatura"}:
            providers.append("trafilatura")
        elif normalized in {"firecrawl", "firecrawl-scrape"}:
            providers.append("firecrawl")
        elif normalized == "browserless":
            providers.append("browserless")
        elif normalized == "apify":
            providers.append("apify")
        elif normalized == "playwright":
            providers.append("playwright")
        elif normalized in {"crawl4ai", "crawl-4-ai"}:
            providers.append("crawl4ai")
        else:
            raise ValueError(
                "provider must be trafilatura, firecrawl, browserless, "
                "apify, playwright, or crawl4ai"
            )
    return tuple(dict.fromkeys(providers))


def _run_browser_case(
    case: BrowserExtractionCase,
    *,
    providers: Sequence[BrowserProviderName],
    provider_instances: Mapping[str, RenderedPageProvider],
    repeats: int,
    baseline_provider: BrowserProviderName,
) -> BrowserExtractionCaseResult:
    runs: list[BrowserExtractionRunResult] = []
    baseline_scores: list[BrowserExtractionScore] = []
    for run_index in range(1, max(1, repeats) + 1):
        for provider in providers:
            started_at = perf_counter()
            try:
                page = provider_instances[provider].render(case.url, case.timeout_seconds)
            except Exception as exc:
                page = _error_page(
                    provider=provider,
                    url=case.url,
                    error=str(exc),
                    latency_ms=_elapsed_ms(started_at),
                )
            score = score_rendered_page(case, page)
            if provider == baseline_provider:
                baseline_scores.append(score)
            else:
                baseline = baseline_scores[-1] if baseline_scores else None
                score = _score_with_baseline_improvement(score, baseline)
            runs.append(
                BrowserExtractionRunResult(
                    case_id=case.id,
                    provider=provider,
                    run_index=run_index,
                    page=page,
                    score=score,
                )
            )
    return BrowserExtractionCaseResult(case=case, runs=runs, summary=_summarize_case(runs))


def score_rendered_page(
    case: BrowserExtractionCase,
    page: RenderedPage,
) -> BrowserExtractionScore:
    """Score one rendered page result for Keystone-agent usefulness."""

    text = " ".join(
        item
        for item in (
            page.title,
            page.text_or_markdown,
            " ".join(link.text for link in page.links),
            " ".join(link.url for link in page.links),
            page.error or "",
        )
        if item
    )
    expected_hits = _signal_hits(text, case.expected_signals)
    forbidden_signals = tuple(dict.fromkeys([*case.forbidden_signals, *DEFAULT_FORBIDDEN_SIGNALS]))
    forbidden_hits = _signal_hits(text, forbidden_signals)
    access_blocked = bool(forbidden_hits) or page.status in {"error", "blocked", "timeout"}
    recall = len(expected_hits) / len(case.expected_signals) if case.expected_signals else 0.0
    useful_text_length = len(page.text_or_markdown.strip())
    boilerplate_ratio = _boilerplate_ratio(page.text_or_markdown)
    quality_bucket = _quality_bucket(
        status=page.status,
        access_blocked=access_blocked,
        expected_signal_recall=round(recall, 3),
        useful_text_length=useful_text_length,
        boilerplate_ratio=boilerplate_ratio,
        has_expected_signals=bool(case.expected_signals),
    )
    return BrowserExtractionScore(
        provider=page.provider,
        status=page.status,
        extraction_success=page.status == "success" and useful_text_length > 0,
        expected_signal_recall=round(recall, 3),
        expected_signal_hits=expected_hits,
        forbidden_signal_hits=forbidden_hits,
        access_blocked=access_blocked,
        title_present=bool(page.title.strip()),
        useful_text_length=useful_text_length,
        boilerplate_ratio=boilerplate_ratio,
        useful_internal_link_count=_useful_internal_link_count(page.links, case.expected_signals),
        latency_ms=page.latency_ms,
        error=page.error,
        quality_bucket=quality_bucket,
        quality_diagnosis=_quality_diagnosis(
            quality_bucket=quality_bucket,
            expected_signals=case.expected_signals,
            expected_hits=expected_hits,
            forbidden_hits=forbidden_hits,
            title_present=bool(page.title.strip()),
            useful_text_length=useful_text_length,
            boilerplate_ratio=boilerplate_ratio,
            page_error=page.error,
        ),
    )


def _score_with_baseline_improvement(
    score: BrowserExtractionScore,
    baseline: BrowserExtractionScore | None,
) -> BrowserExtractionScore:
    if baseline is None:
        return score
    recall_gain = round(score.expected_signal_recall - baseline.expected_signal_recall, 3)
    text_gain = score.useful_text_length - baseline.useful_text_length
    link_gain = score.useful_internal_link_count - baseline.useful_internal_link_count
    improved = bool(
        score.extraction_success
        and (
            (not baseline.extraction_success)
            or recall_gain > 0
            or text_gain >= 1000
            or link_gain > 0
        )
    )
    return score.model_copy(
        update={
            "improvement_over_baseline": {
                "baseline_provider": baseline.provider,
                "improved": improved,
                "expected_signal_recall_gain": recall_gain,
                "useful_text_length_gain": text_gain,
                "useful_internal_link_gain": link_gain,
            }
        }
    )


def _summarize_case(runs: Sequence[BrowserExtractionRunResult]) -> dict[str, Any]:
    return {
        "providers_improved_over_baseline": sum(
            1 for run in runs if bool(run.score.improvement_over_baseline.get("improved"))
        ),
        "weak_or_unreadable_runs": sum(
            1 for run in runs if run.score.quality_bucket in {"weak", "unreadable"}
        ),
        "providers": _summarize_provider_runs(runs),
    }


def _summarize_eval(
    case_results: Sequence[BrowserExtractionCaseResult],
    *,
    baseline_provider: str,
) -> dict[str, Any]:
    runs = [run for case_result in case_results for run in case_result.runs]
    modes: dict[str, int] = defaultdict(int)
    for case_result in case_results:
        modes[case_result.case.mode] += 1
    return {
        "baseline_provider": baseline_provider,
        "case_modes": dict(modes),
        "provider_specs": {
            provider: spec.model_dump(mode="json")
            for provider, spec in build_browser_provider_diagnostic_specs().items()
        },
        "providers": _summarize_provider_runs(runs),
    }


def _summarize_provider_runs(
    runs: Sequence[BrowserExtractionRunResult],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[BrowserExtractionRunResult]] = defaultdict(list)
    for run in runs:
        grouped[run.provider].append(run)
    summary: dict[str, dict[str, Any]] = {}
    for provider, provider_runs in grouped.items():
        scores = [run.score for run in provider_runs]
        latencies = [score.latency_ms for score in scores]
        text_lengths = [score.useful_text_length for score in scores]
        success_count = sum(1 for score in scores if score.extraction_success)
        error_count = sum(1 for score in scores if score.status == "error" or score.error)
        blocked_count = sum(1 for score in scores if score.access_blocked)
        strong_count = sum(1 for score in scores if score.quality_bucket == "strong")
        weak_count = sum(1 for score in scores if score.quality_bucket in {"weak", "unreadable"})
        improved_count = sum(
            1 for score in scores if bool(score.improvement_over_baseline.get("improved"))
        )
        summary[provider] = {
            "run_count": len(provider_runs),
            "success_count": success_count,
            "success_rate": _rate(success_count, len(provider_runs)),
            "error_count": error_count,
            "error_rate": _rate(error_count, len(provider_runs)),
            "blocked_or_forbidden_count": blocked_count,
            "strong_quality_count": strong_count,
            "weak_or_unreadable_count": weak_count,
            "quality_buckets": {
                bucket: sum(1 for score in scores if score.quality_bucket == bucket)
                for bucket in ("strong", "partial", "weak", "unreadable")
            },
            "average_expected_signal_recall": _average(
                score.expected_signal_recall for score in scores
            ),
            "average_useful_text_length": round(_average(text_lengths)),
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "repeatability": _repeatability(provider_runs),
            "improved_over_baseline_count": improved_count,
        }
    return summary


def build_browser_provider_diagnostic_specs() -> dict[str, BrowserProviderDiagnosticSpec]:
    """Return extraction provider role and budget guidance without changing defaults."""

    return {
        "trafilatura": BrowserProviderDiagnosticSpec(
            role="static HTTP extraction baseline",
            promotion_status="baseline",
            readiness="ready as selected-page extraction baseline",
            budget_class="free/local library plus target HTTP request",
            default_use="baseline included for extraction eval comparisons",
            live_requirement="network access for target URL",
            benchmark_focus=(
                "signal recall, useful text length, boilerplate ratio, blocked-page rate"
            ),
            next_validation="rerun as baseline for every selected-page extraction comparison",
            promotion_rule="keep as baseline unless rendered providers consistently outperform it",
        ),
        "firecrawl": BrowserProviderDiagnosticSpec(
            role="managed scrape/extraction fallback",
            promotion_status="explicit_fallback",
            readiness="credential-gated fallback candidate",
            budget_class="configured free-tier or metered",
            default_use="explicit live extraction lane",
            live_requirement="Firecrawl credentials and live flags",
            benchmark_focus="signal recall and blocked-page recovery over Trafilatura",
            next_validation=(
                "small Firecrawl-vs-Trafilatura selected-page probe with explicit "
                "credit budget"
            ),
            promotion_rule="promote only when it improves signal recall or blocked-page access",
        ),
        "browserless": BrowserProviderDiagnosticSpec(
            role="rendered-browser boundary",
            promotion_status="eval_only",
            readiness="placeholder/eval boundary",
            budget_class="metered candidate",
            default_use="eval boundary unless a reviewed adapter is enabled",
            live_requirement="reviewed Browserless adapter and live flags",
            benchmark_focus="JS-heavy selected pages only after adapter review",
            next_validation=(
                "keep out of production until adapter, safety constraints, and "
                "attribution tests exist"
            ),
            promotion_rule="promote only for JS-heavy pages with stable attribution gains",
        ),
        "playwright": BrowserProviderDiagnosticSpec(
            role="local read-only rendered diagnostics",
            promotion_status="diagnostic_only",
            readiness="ready for explicit diagnostics, not routine extraction",
            budget_class="local compute",
            default_use="explicit diagnostics only",
            live_requirement="KEYSTONE_PLAYWRIGHT_ENABLED=true and read-only constraints",
            benchmark_focus="console/page-error/request-failure diagnosis for selected URLs",
            next_validation=(
                "use only after static extraction is weak or rendered diagnostics "
                "are requested"
            ),
            promotion_rule="keep as diagnostics unless runtime policy approves rendered extraction",
        ),
        "apify": BrowserProviderDiagnosticSpec(
            role="future actor-based extraction boundary",
            promotion_status="future_boundary",
            readiness="not implemented",
            budget_class="metered candidate",
            default_use="unsupported eval boundary",
            live_requirement="reviewed adapter not implemented",
            benchmark_focus="not applicable until adapter exists",
            next_validation="add adapter/tests before any provider comparison",
            promotion_rule="requires adapter, tests, credentials, and attribution checks",
        ),
        "crawl4ai": BrowserProviderDiagnosticSpec(
            role="future local/open-source extraction boundary",
            promotion_status="future_boundary",
            readiness="not implemented",
            budget_class="local or self-hosted candidate",
            default_use="unsupported eval boundary",
            live_requirement="reviewed adapter not implemented",
            benchmark_focus="local extraction lift over Trafilatura after adapter review",
            next_validation="prototype behind eval boundary before runtime promotion",
            promotion_rule="requires adapter, tests, and repeatable quality evidence",
        ),
    }


def _quality_bucket(
    *,
    status: str,
    access_blocked: bool,
    expected_signal_recall: float,
    useful_text_length: int,
    boilerplate_ratio: float,
    has_expected_signals: bool,
) -> str:
    if status != "success" or access_blocked or useful_text_length == 0:
        return "unreadable"
    if has_expected_signals and expected_signal_recall >= 0.8 and boilerplate_ratio < 0.5:
        return "strong"
    if useful_text_length >= 800 and boilerplate_ratio < 0.7:
        return "partial"
    return "weak"


def _quality_diagnosis(
    *,
    quality_bucket: str,
    expected_signals: Sequence[str],
    expected_hits: Sequence[str],
    forbidden_hits: Sequence[str],
    title_present: bool,
    useful_text_length: int,
    boilerplate_ratio: float,
    page_error: str | None,
) -> list[str]:
    diagnosis: list[str] = []
    if page_error:
        diagnosis.append(page_error)
    missing_signals = [signal for signal in expected_signals if signal not in expected_hits]
    if missing_signals:
        diagnosis.append("missing expected signals: " + ", ".join(missing_signals[:5]))
    if forbidden_hits:
        diagnosis.append("blocked or forbidden signals: " + ", ".join(forbidden_hits[:5]))
    if not title_present:
        diagnosis.append("page title missing")
    if useful_text_length < 800:
        diagnosis.append("low useful text length")
    if boilerplate_ratio >= 0.7:
        diagnosis.append("high boilerplate ratio")
    if not diagnosis and quality_bucket == "strong":
        diagnosis.append("strong extracted evidence")
    return list(dict.fromkeys(diagnosis))


def _providers_with_baseline(
    providers: Sequence[BrowserProviderName],
    baseline_provider: BrowserProviderName,
) -> tuple[BrowserProviderName, ...]:
    ordered = [baseline_provider, *providers]
    return tuple(dict.fromkeys(ordered))


def _error_page(*, provider: str, url: str, error: str, latency_ms: int) -> RenderedPage:
    return RenderedPage(
        provider=provider,
        url=url,
        final_url=url,
        status="error",
        latency_ms=latency_ms,
        error=error,
    )


def _extract_links(html: str, *, base_url: str) -> list[RenderedLink]:
    origin = _origin(base_url)
    links: list[RenderedLink] = []
    for match in re.finditer(
        r"<a\b[^>]*href=[\"'](?P<href>[^\"'#]+)[\"'][^>]*>(?P<text>.*?)</a>",
        html,
        flags=re.I | re.S,
    ):
        url = urljoin(base_url, match.group("href").strip())
        if not url.startswith(("http://", "https://")):
            continue
        text = extract_clean_text(match.group("text"))[:200]
        links.append(RenderedLink(url=url, text=text, internal=_origin(url) == origin))
        if len(links) >= 200:
            break
    return links


def _signal_hits(text: str, signals: Iterable[str]) -> list[str]:
    lowered = text.lower()
    hits = []
    for signal in signals:
        normalized = str(signal or "").strip().lower()
        if normalized and normalized in lowered:
            hits.append(str(signal))
    return list(dict.fromkeys(hits))


def _useful_internal_link_count(
    links: Sequence[RenderedLink],
    expected_signals: Sequence[str],
) -> int:
    signal_hints = [signal.lower() for signal in expected_signals]
    hints = tuple(dict.fromkeys([*INTERNAL_LINK_HINTS, *signal_hints]))
    count = 0
    for link in links:
        if not link.internal:
            continue
        haystack = f"{link.url} {link.text}".lower()
        if any(hint and hint in haystack for hint in hints):
            count += 1
    return count


def _boilerplate_ratio(text: str) -> float:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    if not words:
        return 0.0
    joined = " ".join(words)
    boilerplate_hits = sum(joined.count(term) for term in BOILERPLATE_TERMS)
    return round(min(1.0, boilerplate_hits / max(1, len(words) / 100)), 3)


def _repeatability(runs: Sequence[BrowserExtractionRunResult]) -> dict[str, Any]:
    if len(runs) <= 1:
        return {"runs": len(runs), "stable": True}
    statuses = {run.score.status for run in runs}
    recalls = [run.score.expected_signal_recall for run in runs]
    lengths = [run.score.useful_text_length for run in runs]
    max_length = max(lengths) if lengths else 0
    min_length = min(lengths) if lengths else 0
    length_delta_ratio = (max_length - min_length) / max(max_length, 1)
    stable = (
        len(statuses) == 1 and (max(recalls) - min(recalls) <= 0.1) and length_delta_ratio <= 0.25
    )
    return {
        "runs": len(runs),
        "stable": stable,
        "status_count": len(statuses),
        "recall_range": round(max(recalls) - min(recalls), 3) if recalls else 0,
        "text_length_delta_ratio": round(length_delta_ratio, 3),
    }


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars]


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
    "BASELINE_BROWSER_PROVIDER",
    "DEFAULT_BROWSER_EVAL_CASES_PATH",
    "DEFAULT_BROWSER_EVAL_OUTPUT_DIR",
    "DEFAULT_RENDERED_PAGE_MAX_OUTPUT_CHARS",
    "DEFAULT_RENDERED_PAGE_TIMEOUT_SECONDS",
    "BrowserExtractionCase",
    "BrowserExtractionEvalOptions",
    "BrowserExtractionEvalReport",
    "BrowserExtractionRunResult",
    "BrowserExtractionScore",
    "BrowserProviderDiagnosticSpec",
    "BrowserlessRenderedPageProvider",
    "DryRunRenderedPageProvider",
    "FirecrawlRenderedPageProvider",
    "PlaywrightRenderedPageProvider",
    "RenderedLink",
    "RenderedPage",
    "RenderedPageProvider",
    "TrafilaturaRenderedPageProvider",
    "UnsupportedRenderedPageProvider",
    "build_browser_provider_diagnostic_specs",
    "build_rendered_page_provider",
    "load_browser_extraction_cases",
    "normalize_browser_providers",
    "render_browser_extraction_eval_report",
    "run_browser_extraction_eval",
    "score_rendered_page",
    "write_browser_extraction_artifacts",
]
