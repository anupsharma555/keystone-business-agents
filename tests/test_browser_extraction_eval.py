from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from keystone_agents.browser_extraction_eval import (
    BrowserExtractionCase,
    BrowserExtractionEvalOptions,
    PlaywrightRenderedPageProvider,
    RenderedLink,
    RenderedPage,
    build_browser_provider_diagnostic_specs,
    build_rendered_page_provider,
    load_browser_extraction_cases,
    normalize_browser_providers,
    render_browser_extraction_eval_report,
    run_browser_extraction_eval,
    score_rendered_page,
    write_browser_extraction_artifacts,
)


def test_load_browser_extraction_cases_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "wide-directory",
                "mode": "wide",
                "url": "https://example.com/directory",
                "category": "directory",
                "difficulty_tags": ["static"],
                "expected_signals": ["grant"],
                "forbidden_signals": ["captcha"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = load_browser_extraction_cases(path)

    assert len(cases) == 1
    assert cases[0].id == "wide-directory"
    assert cases[0].mode == "wide"


def test_score_rendered_page_measures_agent_useful_signals() -> None:
    case = BrowserExtractionCase(
        id="specific-trial",
        mode="specific",
        url="https://example.com/trial",
        category="clinical_trial_record",
        expected_signals=["clinical trial", "depression"],
        forbidden_signals=["captcha"],
    )
    page = RenderedPage(
        provider="fake",
        url=case.url,
        status="success",
        title="Clinical trial page",
        text_or_markdown=("This clinical trial studies depression. Privacy policy. Cookie notice."),
        links=[
            RenderedLink(
                url="https://example.com/trial/about",
                text="About this clinical trial",
                internal=True,
            )
        ],
        latency_ms=123,
    )

    score = score_rendered_page(case, page)

    assert score.extraction_success is True
    assert score.expected_signal_recall == 1.0
    assert score.title_present is True
    assert score.useful_internal_link_count == 1
    assert score.boilerplate_ratio > 0
    assert score.quality_bucket == "weak"
    assert "low useful text length" in score.quality_diagnosis


def test_score_rendered_page_marks_strong_extracted_evidence() -> None:
    case = BrowserExtractionCase(
        id="specific-trial-strong",
        mode="specific",
        url="https://example.com/trial",
        category="clinical_trial_record",
        expected_signals=["clinical trial", "depression"],
    )
    page = RenderedPage(
        provider="fake",
        url=case.url,
        status="success",
        title="Clinical trial page",
        text_or_markdown=" ".join(
            ["This clinical trial studies depression with a structured digital intervention."]
            * 25
        ),
        latency_ms=123,
    )

    score = score_rendered_page(case, page)

    assert score.quality_bucket == "strong"
    assert score.quality_diagnosis == ["strong extracted evidence"]


def test_score_rendered_page_detects_forbidden_or_blocked_signals() -> None:
    case = BrowserExtractionCase(
        id="blocked",
        mode="wide",
        url="https://example.com",
        category="directory",
        forbidden_signals=["captcha"],
    )
    page = RenderedPage(
        provider="fake",
        url=case.url,
        status="success",
        text_or_markdown="Please complete captcha to continue.",
    )

    score = score_rendered_page(case, page)

    assert score.access_blocked is True
    assert "captcha" in [item.lower() for item in score.forbidden_signal_hits]
    assert score.quality_bucket == "unreadable"
    assert any("blocked or forbidden" in item for item in score.quality_diagnosis)


def test_run_eval_compares_provider_against_trafilatura_baseline() -> None:
    case = BrowserExtractionCase(
        id="focused-company",
        mode="focused",
        url="https://example.com",
        category="company_homepage",
        expected_signals=["clinical", "research"],
    )

    @dataclass(frozen=True)
    class FakeProvider:
        provider_name: str
        dry_run: bool = False

        def render(self, url: str, timeout_seconds: int) -> RenderedPage:
            if self.provider_name == "trafilatura":
                text = "Clinical homepage."
            else:
                text = "Clinical research homepage with additional partner details."
            return RenderedPage(
                provider=self.provider_name,
                url=url,
                status="success",
                text_or_markdown=text,
                latency_ms=timeout_seconds,
            )

    def fake_factory(provider, **_kwargs):
        return FakeProvider(provider_name=provider)

    report = run_browser_extraction_eval(
        [case],
        options=BrowserExtractionEvalOptions(providers=("browserless",), live=True),
        provider_factory=fake_factory,
    )

    runs = report.results[0].runs
    browserless_score = next(run.score for run in runs if run.provider == "browserless")
    assert report.providers == ["trafilatura", "browserless"]
    assert browserless_score.improvement_over_baseline["improved"] is True
    assert report.summary["providers"]["browserless"]["improved_over_baseline_count"] == 1
    assert "provider_specs" in report.summary


def test_browser_eval_report_includes_quality_and_provider_specs() -> None:
    case = BrowserExtractionCase(
        id="dry",
        mode="focused",
        url="https://example.com",
        category="company_homepage",
        expected_signals=["leadership"],
    )
    report = run_browser_extraction_eval(
        [case],
        options=BrowserExtractionEvalOptions(providers=("firecrawl",), live=False),
    )

    assert report.summary["provider_specs"]["trafilatura"]["budget_class"].startswith("free")
    assert report.summary["provider_specs"]["firecrawl"]["promotion_status"] == (
        "explicit_fallback"
    )
    assert report.summary["provider_specs"]["firecrawl"]["readiness"] == (
        "credential-gated fallback candidate"
    )
    assert "Firecrawl-vs-Trafilatura" in report.summary["provider_specs"]["firecrawl"][
        "next_validation"
    ]
    assert report.summary["providers"]["trafilatura"]["weak_or_unreadable_count"] == 1
    rendered = render_browser_extraction_eval_report(report)
    assert "Provider Roles" in rendered
    assert "weak/unreadable" in rendered
    assert "readiness:" in rendered


def test_browser_provider_diagnostic_specs_keep_firecrawl_explicit() -> None:
    specs = build_browser_provider_diagnostic_specs()

    assert "explicit" in specs["firecrawl"].default_use
    assert specs["browserless"].promotion_status == "eval_only"
    assert specs["browserless"].readiness == "placeholder/eval boundary"
    assert specs["trafilatura"].role == "static HTTP extraction baseline"
    assert "every selected-page extraction comparison" in specs["trafilatura"].next_validation
    assert "console/page-error/request-failure" in specs["playwright"].benchmark_focus
    assert "diagnostics" in specs["playwright"].readiness


def test_browser_provider_normalizer_accepts_current_and_future_boundaries() -> None:
    assert normalize_browser_providers(
        ["trafilatura", "firecrawl", "browserless", "apify", "playwright", "crawl4ai"]
    ) == (
        "trafilatura",
        "firecrawl",
        "browserless",
        "apify",
        "playwright",
        "crawl4ai",
    )

    with pytest.raises(ValueError, match="provider must be"):
        normalize_browser_providers(["not-a-provider"])


def test_playwright_provider_builds_only_when_selected_and_live(monkeypatch) -> None:
    dry_provider = build_rendered_page_provider("playwright", live=False)
    live_provider = build_rendered_page_provider("playwright", live=True)

    assert dry_provider.render("https://example.com", 1).status == "dry-run"
    assert isinstance(live_provider, PlaywrightRenderedPageProvider)
    monkeypatch.delenv("KEYSTONE_PLAYWRIGHT_ENABLED", raising=False)
    blocked = live_provider.render("https://example.com", 1)
    assert blocked.status == "blocked"
    assert "KEYSTONE_PLAYWRIGHT_ENABLED=true" in str(blocked.error)


def test_write_browser_eval_artifacts(tmp_path: Path) -> None:
    case = BrowserExtractionCase(
        id="dry",
        mode="wide",
        url="https://example.com",
        category="directory",
    )
    report = run_browser_extraction_eval(
        [case],
        options=BrowserExtractionEvalOptions(providers=("browserless",), live=False),
    )

    paths = write_browser_extraction_artifacts(report, tmp_path)

    assert Path(paths["summary"]).is_file()
    assert Path(paths["results"]).is_file()
    assert Path(paths["raw_dir"]).is_dir()
