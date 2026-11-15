from __future__ import annotations

import json
import os

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


def test_cli_loads_selected_local_env_without_overriding_explicit_database(
    tmp_path, monkeypatch, capsys,
) -> None:
    env_database = _database_url(tmp_path)
    explicit_database = f"sqlite:///{tmp_path / 'explicit.db'}"
    store = SQLiteStore(env_database)
    for feed, url in (
        ("rss", "https://example.test/rss"),
        ("preprints", "https://example.test/preprint"),
    ):
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                title=f"Synthetic {feed}", url=url, source="fixture",
                feed=feed, selected=True, summary="Synthetic validation context.",
            )
        )
    (tmp_path / ".env").write_text(f"DATABASE_URL={env_database}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KEYSTONE_CONTEXT_CONFIG_REPO", raising=False)
    monkeypatch.delenv("KEYSTONE_SLACK_REPO", raising=False)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(tmp_path / "absent-discovery.db"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "0")

    assert main([]) == 0
    from_env = json.loads(capsys.readouterr().out)
    assert from_env["operational_ready"] is True
    assert from_env["rss"]["item_count"] == 1
    assert from_env["preprints"]["item_count"] == 1
    assert from_env["rss"]["read_sources"] == ["database_url_source:database_url_from_env"]
    assert "DATABASE_URL" not in os.environ

    assert main(["--database-url", explicit_database, "--require-items"]) == 1
    explicit = json.loads(capsys.readouterr().out)
    assert explicit["rss"]["item_count"] == 0
    assert explicit["preprints"]["item_count"] == 0
    assert explicit["rss"]["read_sources"] == ["database_url_source:database_url_arg"]
    assert env_database not in json.dumps(explicit)
    assert "DATABASE_URL" not in os.environ

    monkeypatch.setenv("DATABASE_URL", explicit_database)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    assert main(["--require-items"]) == 1
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["rss"]["item_count"] == 0
    assert disabled["preprints"]["item_count"] == 0
    assert os.environ["DATABASE_URL"] == explicit_database
