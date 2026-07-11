from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.schemas.research import (
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.tools.search_provider import SearchResult
from scripts.run_headway_funding_relevance import (
    build_funding_packet,
    execute_validation,
    normalize_brief_against_funding_packet,
)


def _results() -> list[SearchResult]:
    return [
        SearchResult(
            title="Headway Raises $100 Million in Series D Funding",
            link="https://www.prnewswire.com/news-releases/headway-series-d.html",
            snippet=(
                "NEW YORK, July 23, 2024. Headway announced a $100 million Series D "
                "at a $2.3 billion valuation and planned Medicare Advantage and Medicaid expansion."
            ),
            source="exa",
            date="2026-06-26T12:00:00Z",
        ),
        SearchResult(
            title="Headway Capital closes fifth fund",
            link="https://headwaycap.com/fund-v",
            snippet="Headway Capital raised a private equity fund.",
            source="exa",
            date="2025-01-01T00:00:00Z",
        ),
    ]


def _brief() -> ResearchBrief:
    return ResearchBrief(
        target_name="Headway",
        target_type="company",
        summary=(
            "The latest verified event in the bounded search is Headway's July 2024 "
            "$100 million Series D; it is not recent as of 2026."
        ),
        key_findings=[
            "The round supported Medicare Advantage and Medicaid expansion at a reported "
            "$2.3 billion valuation."
        ],
        facts=[
            ResearchBriefFact(
                text="Headway announced a $100 million Series D on July 23, 2024.",
                source_ids=["headway:funding:1"],
                confidence=0.95,
            )
        ],
        inferences=[
            "This is strategically relevant to KNI's payer and behavioral-health work, "
            "but the old round is not evidence of an immediate opportunity."
        ],
        limitations=["The bounded search found no later verified funding round."],
        sources=[
            ResearchSourceCitation(
                source_id="headway:funding:1",
                title="Headway Raises $100 Million in Series D Funding",
                url="https://www.prnewswire.com/news-releases/headway-series-d.html",
                source_type="company_release",
            )
        ],
    )


class _Provider:
    def search_structured(self, _request):
        return _results()


def test_funding_packet_excludes_wrong_headway_and_marks_staleness() -> None:
    packet = build_funding_packet(
        _results(),
        now=datetime(2026, 7, 11, tzinfo=UTC),
    )

    assert packet["latest_verified_funding_date"] == "2024-07-23"
    assert packet["recent_within_365_days"] is False
    assert len(packet["sources"]) == 1
    assert "headwaycap.com" not in packet["sources"][0]["url"]


def test_funding_brief_drops_provider_date_conflict_commentary() -> None:
    packet = build_funding_packet(_results(), now=datetime(2026, 7, 11, tzinfo=UTC))
    brief = _brief().model_copy(
        update={
            "facts": [
                *_brief().facts,
                ResearchBriefFact(
                    text=(
                        "The bounded source context says the latest verified funding date "
                        "is 2026-06-26, but the event was in 2024."
                    ),
                    source_ids=["headway:funding:1"],
                    confidence=0.5,
                ),
            ],
            "limitations": [
                "The metadata says 'latest_funding_age_days': 15, but it conflicts with 2024."
            ],
        }
    )

    normalized = normalize_brief_against_funding_packet(brief, packet)

    assert all("2026-06-26" not in fact.text for fact in normalized.facts)
    assert all("latest_funding_age_days" not in item for item in normalized.limitations)


def test_headway_funding_validation_passes_one_request_without_tools(tmp_path: Path) -> None:
    result = execute_validation(
        request_text=(
            "Find recent funding news for Headway and summarize whether it is relevant to KNI."
        ),
        model="gpt-5.4-mini",
        budget_usd=0.05,
        output=tmp_path / "result.json",
        now=datetime(2026, 7, 11, tzinfo=UTC),
        search_provider_factory=lambda *_args, **_kwargs: _Provider(),
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=_brief(),
            usage={"requests": 1, "input_tokens": 100, "output_tokens": 50},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
        ),
    )

    assert result["status"] == "pass"
    assert result["retrieval"]["search_requests"] == 1
    assert result["safety"]["tools_attached"] is False
    assert result["usage"]["requests"] == 1


def test_headway_funding_validation_rejects_freshness_overclaim(tmp_path: Path) -> None:
    brief = _brief().model_copy(
        update={
            "summary": "Headway recently raised $100 million in 2024.",
            "limitations": [],
        }
    )
    result = execute_validation(
        request_text="Find recent funding news for Headway and assess KNI relevance.",
        model="gpt-5.4-mini",
        budget_usd=0.05,
        output=tmp_path / "result.json",
        now=datetime(2026, 7, 11, tzinfo=UTC),
        search_provider_factory=lambda *_args, **_kwargs: _Provider(),
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=brief,
            usage={"requests": 1},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
        ),
    )

    assert result["status"] == "partial"
    assert "freshness_caveat" in result["failures"]
