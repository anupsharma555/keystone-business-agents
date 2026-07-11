from __future__ import annotations

from keystone_agents.schemas.announcement_feed import (
    AnnouncementFeedEvidence,
    AnnouncementFeedItem,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from scripts.run_announcement_context_read_validation import main, run_validation


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'announcement-context.db'}"


def test_validation_reports_reachable_but_empty_context(tmp_path, capsys) -> None:
    database_url = _database_url(tmp_path)

    result = run_validation(database_url=database_url)

    assert result["rss"]["tool_status"] == "success"
    assert result["rss"]["usable_context"] is False
    assert result["rss"]["read_sources"] == ["database_url_source:database_url_arg"]
    assert result["preprints"]["tool_status"] == "success"
    assert result["preprints"]["usable_context"] is False
    assert result["operational_ready"] is False
    assert set(result["side_effects"].values()) == {0}
    assert main(["--database-url", database_url, "--require-items"]) == 1
    assert "No matching announcement feed history was found" in capsys.readouterr().out


def test_validation_reports_identity_and_source_evidence_without_content(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Synthetic RSS validation item",
            url="https://example.test/rss-item",
            source="synthetic-rss",
            feed="rss",
            selected=True,
            summary="Synthetic summary used only for validation.",
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    title="Synthetic source",
                    url="https://example.test/rss-item",
                    snippet="Synthetic evidence.",
                    source="fixture",
                    status="success",
                )
            ],
        )
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            title="Synthetic preprint validation item",
            url="https://doi.org/10.0000/synthetic",
            source="synthetic-preprints",
            feed="preprints",
            doi="10.0000/synthetic",
            tags=["preprint"],
            selected=True,
            summary="Synthetic preprint summary used only for validation.",
        )
    )

    result = run_validation(database_url=database_url)

    assert result["operational_ready"] is True
    assert result["rss"]["items_with_identity"] == 1
    assert result["rss"]["items_with_source"] == 1
    assert result["rss"]["evidence_status_counts"] == {"article_extracted": 1}
    assert result["preprints"]["items_with_identity"] == 1
    assert result["preprints"]["items_with_source"] == 1
    assert result["preprints"]["evidence_status_counts"] == {
        "historical_summary_only": 1
    }
    assert "Synthetic" not in str(result)
