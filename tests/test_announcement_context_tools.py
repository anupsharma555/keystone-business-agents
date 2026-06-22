from __future__ import annotations

from pathlib import Path

from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.schemas.operational_context import (
    HistoricalFeedContextItem,
    PreprintsContextResult,
    RssContextResult,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.announcement_context_tools import (
    retrieve_preprint_announcement_history_impl,
    retrieve_rss_announcement_history_impl,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'announcement-context.db'}"


def test_rss_context_history_returns_non_preprint_records(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Digital psychiatry partnership announcement",
            url="https://example.org/partnership",
            source="#announcements",
            feed="rss",
            tags=["partnership", "digital psychiatry"],
            selected=True,
            selection_reason="Useful partnership signal.",
            summary="A partnership announcement relevant to psychiatry operations.",
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="Partnership source",
                    url="https://example.org/partnership",
                    snippet="The source describes a digital psychiatry partnership.",
                    source="trafilatura",
                    status="success",
                    char_count=1200,
                )
            ],
        )
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Depression AI preprint",
            url="https://doi.org/10.1101/2026.01.02.234567",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.01.02.234567",
            tags=["preprint", "depression"],
            selected=True,
            summary="A preprint that should be reserved for preprint context.",
        )
    )

    result = retrieve_rss_announcement_history_impl(
        "digital psychiatry",
        selected_only=True,
        database_url=database_url,
    )

    assert result["status"] == "success"
    assert result["item_count"] == 1
    assert result["items"][0]["title"] == "Digital psychiatry partnership announcement"
    assert result["items"][0]["feed_item_id"]
    assert result["items"][0]["evidence_notes"]
    assert result["items"][0]["detailed_summary_seed"]
    assert result["items"][0]["evidence_status"] == "article_extracted"
    assert "feed=rss" in result["items"][0]["source_basis"]
    assert result["send_enabled"] is False


def test_preprint_context_history_filters_to_preprints(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Depression AI preprint",
            url="https://doi.org/10.1101/2026.01.02.234567",
            source="medRxiv",
            feed="preprints",
            doi="10.1101/2026.01.02.234567",
            tags=["preprint", "depression"],
            selected=True,
            selection_reason="Relevant to psychiatry AI review.",
            summary="A preprint about AI-supported depression assessment.",
        )
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="General payer announcement",
            url="https://example.org/payer",
            source="#announcements",
            feed="rss",
            tags=["payer"],
            selected=True,
            summary="A non-preprint announcement.",
        )
    )

    result = retrieve_preprint_announcement_history_impl(
        "depression assessment",
        selected_only=True,
        database_url=database_url,
    )

    assert result["status"] == "success"
    assert result["item_count"] == 1
    assert result["kind"] == "preprints"
    assert result["items"][0]["title"] == "Depression AI preprint"
    assert result["items"][0]["source_ids"][1] == "10.1101/2026.01.02.234567"
    assert result["items"][0]["publication_ids"] == ["10.1101/2026.01.02.234567"]
    assert result["items"][0]["evidence_status"] == "historical_summary_only"


def test_historical_feed_context_schema_supports_frontier_synthesis() -> None:
    item = HistoricalFeedContextItem(
        feed_item_id="doi:10.1101/example",
        title="AI psychiatry preprint",
        detailed_summary="The preprint tests a behavioral-health triage model.",
        source_basis="feed=preprints; evidence_status=historical_summary_only",
        key_findings=["Model evaluation is relevant to clinical workflow triage."],
        limitations=["Preprint evidence is preliminary."],
        relevance_to_psychiatry="Evaluates a digital psychiatry workflow.",
        relevance_to_keystone="Useful for monitoring clinical AI service opportunities.",
        frontier_signal="Workflow-level AI evaluation in behavioral health.",
        evidence_status="historical_summary_only",
        publication_ids=["10.1101/example"],
    )

    rss = RssContextResult(
        frontier_summary="RSS history points to partner and payer movement.",
        articles=[item],
        research_frontiers=["Measurement-based care operations"],
        clinical_translation_signals=["AI triage moving toward workflow validation"],
        market_or_partnership_signals=["Payer and provider partnerships"],
        evidence_gaps=["Current source verification is still needed."],
        monitoring_queries=["behavioral health AI partnership validation"],
    )
    preprints = PreprintsContextResult(
        frontier_summary="Preprint history points to model evaluation methods.",
        articles=[item],
        research_frontiers=["Foundation-model evaluation for psychiatry"],
        clinical_translation_signals=["Preclinical validation before deployment"],
        evidence_gaps=["Peer review status is unknown."],
        monitoring_queries=["psychiatry AI preprint clinical validation"],
    )

    assert rss.agent_name == "rss_context_agent"
    assert rss.articles[0].frontier_signal
    assert preprints.agent_name == "preprints_context_agent"
    assert preprints.evidence_gaps == ["Peer review status is unknown."]
