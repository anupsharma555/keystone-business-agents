from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import keystone_agents.tools.announcement_context_tools as announcement_tools
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


def test_preprint_context_falls_back_to_linked_discovery_store_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack_repo = tmp_path / "keystone-slack"
    discovery_path = slack_repo / ".local" / "discovery.sqlite"
    discovery_path.parent.mkdir(parents=True)
    with sqlite3.connect(discovery_path) as connection:
        connection.execute(
            """
            CREATE TABLE discovery_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_type TEXT NOT NULL,
                source TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                published TEXT NOT NULL,
                url TEXT NOT NULL,
                topics_json TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO discovery_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "preprint:linked:1",
                "preprint",
                "medRxiv",
                "10.1101/2026.07.09.123456",
                "Clinical AI workflow validation",
                "A preliminary workflow validation study.",
                "2026-07-09",
                "https://doi.org/10.1101/2026.07.09.123456",
                json.dumps(["psychiatry", "clinical AI"]),
                "candidate",
                "2026-07-09T16:00:00Z",
                json.dumps({"doi": "10.1101/2026.07.09.123456"}),
            ),
        )
    (slack_repo / ".env").write_text(
        "DISCOVERY_STORE_PATH=.local/discovery.sqlite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_CONTEXT_CONFIG_REPO", str(slack_repo))
    monkeypatch.delenv("DISCOVERY_STORE_PATH", raising=False)

    result = retrieve_preprint_announcement_history_impl(
        "clinical AI",
        database_url=_database_url(tmp_path),
    )

    assert result["status"] == "success"
    assert result["item_count"] == 1
    assert result["blockers"] == []
    assert result["items"][0]["feed_item_id"] == "preprint:linked:1"
    assert result["items"][0]["publication_ids"] == [
        "10.1101/2026.07.09.123456"
    ]
    assert result["items"][0]["evidence_status"] == "discovery_candidate"
    assert result["items"][0]["selected"] is False
    assert any(
        item["key"] == "linked_discovery_store" and item["value"] == "linked_repo_env"
        for item in result["diagnostics"]
    )


def test_preprint_context_does_not_fallback_for_selected_only_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    discovery_path.touch()
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    result = retrieve_preprint_announcement_history_impl(
        selected_only=True,
        database_url=_database_url(tmp_path),
    )

    assert result["item_count"] == 0
    assert result["blockers"] == ["No matching announcement feed history was found."]


def test_rss_context_live_slack_fallback_is_explicit_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED", "true")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_ANNOUNCEMENTS_CHANNEL", "CANNOUNCE")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_slack_api_json(
        endpoint: str,
        *,
        token: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        assert token == "xoxb-test-token"
        calls.append((endpoint, params))
        return {
            "ok": True,
            "messages": [
                {
                    "ts": "1783692000.000100",
                    "text": (
                        "*KNI RSS Digest* | Fri, Jul 10, 10:00 AM ET | 1 selected\n\n"
                        "*1.*\n"
                        "*Title:* <https://example.test/clinical-ai|Clinical AI update>\n"
                        "*Date:* Fri, Jul 10, 9:00 AM EDT\n"
                        "*Feed source:* Example Health News\n"
                        "*Category:* Healthcare AI Implementation\n"
                        "*Link:* <https://example.test/clinical-ai>\n"
                        "Why relevant: Healthcare AI: implementation and workflow"
                    ),
                }
            ],
        }

    monkeypatch.setattr(announcement_tools, "_slack_api_json", fake_slack_api_json)

    result = retrieve_rss_announcement_history_impl(
        "clinical AI",
        database_url=_database_url(tmp_path),
        live=True,
    )

    assert calls == [
        (
            "conversations.history",
            {"channel": "CANNOUNCE", "limit": 32},
        )
    ]
    assert result["item_count"] == 1
    assert result["blockers"] == []
    assert result["items"][0]["feed_item_id"] == (
        "slack:CANNOUNCE:1783692000.000100:1"
    )
    assert result["items"][0]["url"] == "https://example.test/clinical-ai"
    assert result["items"][0]["evidence_status"] == "slack_digest_history"
    assert result["items"][0]["selected"] is True
    assert result["send_enabled"] is False


def test_rss_context_live_slack_fallback_requires_process_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("SLACK_ANNOUNCEMENTS_CHANNEL", "CANNOUNCE")
    monkeypatch.setattr(
        announcement_tools,
        "_slack_api_json",
        lambda *_args, **_kwargs: pytest.fail("Slack API must not run without the gate"),
    )

    result = retrieve_rss_announcement_history_impl(
        database_url=_database_url(tmp_path),
        live=True,
    )

    assert result["item_count"] == 0
    assert result["blockers"] == ["No matching announcement feed history was found."]
    assert any(
        item["key"] == "slack_rss_history" and item["value"] == "blocked"
        for item in result["diagnostics"]
    )


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
