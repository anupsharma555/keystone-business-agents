from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import keystone_agents.tools.announcement_context_tools as announcement_tools
from keystone_agents import canary_acceptance
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
    read_preprint_announcement_evidence_impl,
    read_rss_announcement_evidence_impl,
    retrieve_preprint_announcement_history_impl,
    retrieve_rss_announcement_history_impl,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'announcement-context.db'}"


def _synthetic_linked_discovery_store(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE discovery_candidates (
                candidate_id TEXT PRIMARY KEY,
                candidate_type TEXT NOT NULL,
                source TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                published TEXT NOT NULL,
                url TEXT NOT NULL,
                topics_json TEXT,
                status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                metadata_json TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO discovery_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "synthetic:preprint:latest",
                    "preprint",
                    "Example Preprints",
                    "synthetic-001",
                    "Clinical-AI response study",
                    "Depression outcomes were evaluated in a synthetic cohort.",
                    "2026-08-03",
                    "https://example.test/preprints/synthetic-001",
                    json.dumps(["neuromodulation", "workflow evaluation"]),
                    "candidate",
                    "2026-08-03T12:00:00Z",
                    json.dumps({"doi": "10.0000/synthetic-001"}),
                ),
                (
                    "synthetic:preprint:older",
                    "preprint",
                    "Example Preprints",
                    "synthetic-002",
                    "Neuromodulation methods",
                    "A depression follow-up study with synthetic evidence.",
                    "2026-08-02",
                    "https://example.test/preprints/synthetic-002",
                    None,
                    "candidate",
                    "2026-08-02T12:00:00Z",
                    "{}",
                ),
                (
                    "synthetic:preprint:sparse",
                    "preprint",
                    "Example Preprints",
                    "synthetic-003",
                    "Sparse metadata record",
                    None,
                    "2026-08-01",
                    "https://example.test/preprints/synthetic-003",
                    None,
                    "candidate",
                    "2026-08-01T12:00:00Z",
                    None,
                ),
                (
                    "synthetic:web:newer",
                    "web_result",
                    "Example Web",
                    "synthetic-web-001",
                    "Depression neuromodulation overview",
                    "A newer non-preprint record.",
                    "2026-08-04",
                    "https://example.test/web/synthetic-001",
                    json.dumps(["clinical AI"]),
                    "candidate",
                    "2026-08-04T12:00:00Z",
                    "{}",
                ),
            ],
        )


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


@pytest.mark.parametrize("kind", ["rss", "preprints"])
def test_canonical_history_indexes_and_reads_fourth_saved_evidence(
    tmp_path: Path,
    kind: str,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = AnnouncementFeedItem(
        canonical_key=f"fourth-{kind}",
        title=f"{kind} synthetic evidence ordering",
        url="https://example.org/review",
        source=kind,
        feed=kind,
        summary="A preliminary research preview.",
        published_at="2026-09-01",
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    title=f"Snippet {index}",
                    url=f"https://example.org/snippet-{index}",
                    snippet="Background context only.",
                )
                for index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                title="Full results",
                url="https://example.org/results",
                source="saved extractor",
                snippet=(
                    "The primary outcome was NOT improved; external validation is absent."
                ),
                status="success",
                metadata={"version": "v4", "published_at": "2026-09-01"},
            ),
        ],
    )
    store.save_announcement_feed_item(item)
    history_reader = (
        retrieve_preprint_announcement_history_impl
        if kind == "preprints"
        else retrieve_rss_announcement_history_impl
    )
    evidence_reader = (
        read_preprint_announcement_evidence_impl
        if kind == "preprints"
        else read_rss_announcement_evidence_impl
    )

    history = history_reader(
        query=f"{kind} synthetic evidence ordering",
        database_url=database_url,
    )
    candidate = history["items"][0]
    fourth = candidate["evidence_index"][3]
    evidence = evidence_reader(
        feed_item_id=candidate["feed_item_id"],
        evidence_id=fourth["evidence_id"],
        database_url=database_url,
    )

    assert candidate["evidence_count"] == 4
    assert candidate["selected_evidence_read_required"] is True
    assert candidate["article_full_text_verified"] is False
    assert fourth == {
        "evidence_id": fourth["evidence_id"],
        "position": 4,
        "kind": "article",
        "title": "Full results",
        "url": "https://example.org/results",
        "url_complete": True,
        "preview_only": True,
        "authoritative_metadata_in_selected_read": True,
        "source": "saved extractor",
        "status": "success",
        "saved_content_scope": "saved_evidence_snippet",
        "saved_content_available": True,
        "saved_content_char_count": 68,
        "reported_source_char_count": 0,
        "metadata_keys": ["published_at", "version"],
    }
    assert evidence["status"] == "success"
    assert '"version":"v4"' in evidence["metadata_text"]
    assert '"published_at":"2026-09-01"' in evidence["metadata_text"]
    assert "NOT improved" in evidence["text"]
    assert evidence["saved_content_scope"] == "saved_evidence_snippet"
    assert evidence["article_full_text_verified"] is False
    assert evidence["send_enabled"] is False


def test_saved_evidence_continuation_has_no_gaps_and_rejects_snapshot_change(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    full_text = (
        ("Background review detail. " * 90)
        + "NOT approved for clinical use; only a single-site pilot."
    )
    item = AnnouncementFeedItem(
        canonical_key="long-evidence",
        title="Long source retention",
        url="https://example.org/long-preview",
        source="rss",
        feed="rss",
        summary="Preliminary research.",
        evidence=[
            AnnouncementFeedEvidence(
                kind="article",
                url="https://example.org/full-results",
                title="Detailed findings",
                snippet=full_text,
                status="success",
            )
        ],
    )
    store.save_announcement_feed_item(item)
    history = retrieve_rss_announcement_history_impl(
        query="Long source retention",
        database_url=database_url,
    )
    candidate = history["items"][0]
    evidence_id = candidate["evidence_index"][0]["evidence_id"]

    pages: list[dict[str, object]] = []
    request: dict[str, object] = {
        "feed_item_id": "long-evidence",
        "evidence_id": evidence_id,
        "max_chars": 1000,
    }
    while True:
        page = read_rss_announcement_evidence_impl(
            database_url=database_url,
            **request,  # type: ignore[arg-type]
        )
        pages.append(page)
        if not page["continuation"]["available"]:  # type: ignore[index]
            break
        request = dict(page["continuation"]["next_request"])  # type: ignore[arg-type,index]

    repeated = read_rss_announcement_evidence_impl(
        feed_item_id="long-evidence",
        evidence_id=evidence_id,
        max_chars=1000,
        database_url=database_url,
    )
    combined = "".join(str(page["text"]) for page in pages)

    changed = item.model_copy(deep=True)
    changed.evidence[0].snippet = full_text.replace("single-site", "multisite")
    store.save_announcement_feed_item(changed)
    changed_result = read_rss_announcement_evidence_impl(
        database_url=database_url,
        **pages[0]["continuation"]["next_request"],  # type: ignore[arg-type,index]
    )

    assert combined == full_text
    assert "NOT approved for clinical use" in combined
    assert all(
        pages[index]["read_window"]["end_char"]  # type: ignore[index]
        == pages[index + 1]["read_window"]["start_char"]  # type: ignore[index]
        for index in range(len(pages) - 1)
    )
    assert repeated["text"] == pages[0]["text"]
    assert repeated["read_window"] == pages[0]["read_window"]
    assert changed_result["status"] == "source_changed"
    assert changed_result["text"] == ""


def test_saved_evidence_index_continuation_and_empty_records_are_explicit(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    evidence = [
        AnnouncementFeedEvidence(
            kind="search" if index % 2 == 0 else "article",
            title=f"Evidence {index}",
            url=f"https://example.org/evidence/{index}",
            snippet=f"Saved evidence {index}",
            status="success",
        )
        for index in range(13)
    ]
    evidence.append(
        AnnouncementFeedEvidence(
            kind="article",
            title="Unavailable record",
            url="https://example.org/evidence/unavailable",
            snippet="",
            status="failed",
        )
    )
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            canonical_key="many-evidence",
            title="Many evidence records",
            url="https://example.org/many",
            source="rss",
            feed="rss",
            evidence=evidence,
        )
    )

    history = retrieve_rss_announcement_history_impl(
        query="Many evidence records",
        database_url=database_url,
    )
    candidate = history["items"][0]
    first_ids = [item["evidence_id"] for item in candidate["evidence_index"]]
    index_next = candidate["evidence_index_coverage"]["next_request"]
    second = read_rss_announcement_evidence_impl(
        database_url=database_url,
        **index_next,
    )
    second_ids = [item["evidence_id"] for item in second["evidence_index"]]
    unavailable_id = second["evidence_index"][-1]["evidence_id"]
    unavailable = read_rss_announcement_evidence_impl(
        feed_item_id="many-evidence",
        evidence_id=unavailable_id,
        database_url=database_url,
    )
    absent = read_rss_announcement_evidence_impl(
        feed_item_id="many-evidence",
        evidence_id="evidence:not-present",
        database_url=database_url,
    )

    assert candidate["evidence_count"] == 14
    assert candidate["evidence_index_coverage"]["has_more"] is True
    assert second["read_mode"] == "evidence_index"
    assert second["evidence_index_coverage"]["has_more"] is False
    assert len(first_ids + second_ids) == len(set(first_ids + second_ids)) == 14
    assert unavailable["status"] == "success"
    assert unavailable["content_status"] == "unavailable_saved_record"
    assert unavailable["text"] == ""
    assert absent["status"] == "not_found"
    assert absent["content_status"] == "evidence_identity_not_found"


def test_preprint_selected_evidence_read_preserves_public_snapshot_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url).save_announcement_feed_item(
        AnnouncementFeedItem(
            canonical_key="canonical-only-preprint",
            title="Canonical-only preprint",
            url="https://example.org/canonical-only",
            source="preprints",
            feed="preprints",
            evidence=[
                AnnouncementFeedEvidence(
                    kind="article",
                    snippet="Canonical content must not cross the snapshot boundary.",
                )
            ],
        )
    )
    monkeypatch.setenv("KEYSTONE_CANARY_ACCEPTANCE_PROFILE", "public-preprints")
    monkeypatch.setattr(
        canary_acceptance,
        "public_preprint_snapshot_binding",
        lambda: (tmp_path / "public-snapshot.sqlite", "a" * 64),
    )

    result = read_preprint_announcement_evidence_impl(
        feed_item_id="canonical-only-preprint",
        evidence_id="evidence:any",
        database_url=database_url,
    )

    assert result["status"] == "source_scope_mismatch"
    assert result["text"] == ""
    assert "Canonical content" not in json.dumps(result)
    assert any("public preprint snapshot" in item for item in result["limitations"])


def test_selected_evidence_bounds_metadata_and_recovers_exact_long_url(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    exact_url = "https://example.org/" + ("u" * 1500) + "?citation=exact-tail"
    item = AnnouncementFeedItem(
        canonical_key="bounded-metadata",
        title="Bounded metadata",
        url="https://example.org/item",
        source="rss",
        feed="rss",
        evidence=[
            AnnouncementFeedEvidence(
                kind="article",
                title="Long metadata record",
                url=exact_url,
                snippet="NOT approved for clinical use.",
                status="success",
                metadata={"large_context": "m" * 135_000, "version": "v7"},
            )
        ],
    )
    SQLiteStore(database_url).save_announcement_feed_item(item)
    history = retrieve_rss_announcement_history_impl(
        query=item.title,
        database_url=database_url,
    )
    evidence_id = history["items"][0]["evidence_index"][0]["evidence_id"]
    assert history["items"][0]["evidence_index"][0]["url_complete"] is False

    pages: list[dict[str, object]] = []
    request: dict[str, object] = {
        "feed_item_id": item.canonical_key,
        "evidence_id": evidence_id,
        "max_chars": 500,
    }
    for _ in range(6):
        page = read_rss_announcement_evidence_impl(
            database_url=database_url,
            **request,  # type: ignore[arg-type]
        )
        pages.append(page)
        if "citation=exact-tail" in "".join(
            str(value["metadata_text"]) for value in pages
        ):
            break
        request = dict(page["continuation"]["next_request"])  # type: ignore[arg-type,index]

    first = pages[0]
    recovered_metadata = "".join(str(page["metadata_text"]) for page in pages)
    assert first["text"] == "NOT approved for clinical use."
    assert len(str(first["metadata_text"])) <= 500
    assert len(json.dumps(first)) < 10_000
    assert first["metadata_window"]["has_more"] is True  # type: ignore[index]
    assert "citation=exact-tail" in recovered_metadata
    assert any("exact URL" in item for item in first["limitations"])


@pytest.mark.parametrize("kind", ["rss", "preprints"])
def test_legacy_first_three_fts_index_still_finds_later_saved_evidence(
    tmp_path: Path,
    kind: str,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = AnnouncementFeedItem(
        canonical_key=f"legacy-index-{kind}",
        title=f"{kind} source overview",
        url=f"https://example.org/{kind}",
        source=kind,
        feed=kind,
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    title="Background",
                    url=f"https://example.org/background/{index}",
                    snippet="General source context.",
                )
                for index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                title="Validation detail",
                url="https://example.org/evidence",
                snippet="Rivermark validation is NOT complete.",
                status="success",
            ),
        ],
    )
    store.save_announcement_feed_item(item)
    database_path = tmp_path / "announcement-context.db"
    legacy_index_text = "\n".join(
        [
            item.title,
            " ".join(evidence.snippet for evidence in item.evidence[:3]),
        ]
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE announcement_feed_fts SET retrieval_text = ? "
            "WHERE canonical_key = ?",
            (legacy_index_text, item.canonical_key),
        )

    results = store.retrieve_announcement_feed_items(
        query="Rivermark",
        source=kind,
    )

    assert [result.canonical_key for result in results] == [item.canonical_key]
    assert "Rivermark" in item.semantic_index_text()


@pytest.mark.parametrize("kind", ["rss", "preprints"])
@pytest.mark.parametrize(
    ("decoy_count", "target_first"),
    [(65, True), (130, False)],
)
def test_legacy_dense_decoys_rank_complete_term_coverage_before_candidate_limit(
    tmp_path: Path,
    kind: str,
    decoy_count: int,
    target_first: bool,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    target = AnnouncementFeedItem(
        canonical_key=f"dense-legacy-target-{kind}-{decoy_count}-{target_first}",
        title="Rivermark outcome record",
        summary="Saved research.",
        source=kind,
        feed=kind,
        published_at="2026-01-01",
        selected=True,
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    snippet="General background only.",
                )
                for _index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                snippet="Validation is NOT complete.",
                url="https://example.org/dense-target/source",
            ),
        ],
    )

    def save_target_with_legacy_index() -> None:
        store.save_announcement_feed_item(target)
        with store.managed_connection() as connection:
            connection.execute(
                "UPDATE announcement_feed_fts SET retrieval_text = ? "
                "WHERE canonical_key = ?",
                ("Legacy background without the query terms.", target.canonical_key),
            )

    if target_first:
        save_target_with_legacy_index()
    for index in range(decoy_count):
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                canonical_key=f"dense-{kind}-decoy-{decoy_count}-{index}",
                title=f"Validation memo {index}",
                summary="Unrelated background validation.",
                source=kind,
                feed=kind,
                published_at="2026-09-01",
                selected=True,
                evidence=[
                    AnnouncementFeedEvidence(
                        kind="article",
                        snippet="Unrelated validation background.",
                    )
                ],
            )
        )
    if not target_first:
        save_target_with_legacy_index()

    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            canonical_key=f"dense-{kind}-unselected-specific",
            title="Rivermark validation unselected",
            source=kind,
            feed=kind,
            published_at="2026-10-01",
            selected=False,
        )
    )
    other_kind = "preprints" if kind == "rss" else "rss"
    store.save_announcement_feed_item(
        AnnouncementFeedItem(
            canonical_key=f"dense-{kind}-wrong-source-specific",
            title="Rivermark validation wrong source",
            source=other_kind,
            feed=other_kind,
            published_at="2026-10-01",
            selected=True,
        )
    )

    results = store.retrieve_announcement_feed_items(
        query="Rivermark validation",
        source=kind,
        selected_only=True,
        limit=3,
    )
    result_ids = [result.canonical_key for result in results]
    with store.managed_connection() as connection:
        legacy_index_after = connection.execute(
            "SELECT retrieval_text FROM announcement_feed_fts WHERE canonical_key = ?",
            (target.canonical_key,),
        ).fetchone()[0]

    assert result_ids[0] == target.canonical_key
    assert len(result_ids) == 3
    assert len(set(result_ids)) == 3
    assert all(result.source == kind and result.selected for result in results)
    assert legacy_index_after == "Legacy background without the query terms."


@pytest.mark.parametrize("kind", ["rss", "preprints"])
def test_legacy_dense_decoys_preserve_sql_metadata_term_coverage(
    tmp_path: Path,
    kind: str,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    target = AnnouncementFeedItem(
        canonical_key=f"metadata-legacy-target-{kind}",
        title=f"{kind} overview",
        summary="Saved research.",
        source=kind,
        feed=kind,
        published_at="2026-01-01",
        evidence=[
            *[
                AnnouncementFeedEvidence(
                    kind="search",
                    snippet="General background.",
                )
                for _index in range(3)
            ],
            AnnouncementFeedEvidence(
                kind="article",
                snippet="The outcome is NOT complete.",
                metadata={"topic": "Rivermark validation"},
                url="https://example.org/metadata-target/source",
            ),
        ],
    )
    store.save_announcement_feed_item(target)
    legacy_text = "Legacy background without the metadata query terms."
    with store.managed_connection() as connection:
        connection.execute(
            "UPDATE announcement_feed_fts SET retrieval_text = ? "
            "WHERE canonical_key = ?",
            (legacy_text, target.canonical_key),
        )
    for index in range(65):
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                canonical_key=f"metadata-{kind}-decoy-{index}",
                title=f"Validation memo {index}",
                summary="Unrelated background validation.",
                source=kind,
                feed=kind,
                published_at="2026-09-01",
                evidence=[
                    AnnouncementFeedEvidence(
                        kind="article",
                        snippet="Unrelated validation background.",
                    )
                ],
            )
        )

    results = store.retrieve_announcement_feed_items(
        query="Rivermark validation",
        source=kind,
        limit=3,
    )
    with store.managed_connection() as connection:
        legacy_index_after = connection.execute(
            "SELECT retrieval_text FROM announcement_feed_fts WHERE canonical_key = ?",
            (target.canonical_key,),
        ).fetchone()[0]

    assert results[0].canonical_key == target.canonical_key
    assert len(results) == 3
    assert legacy_index_after == legacy_text


def test_canonical_history_preserves_paraphrased_recall_and_same_title_versions(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    for version, published_at, qualification in (
        ("v1", "2026-08-01", "External validation had NOT occurred."),
        ("v2", "2026-09-01", "External validation remains incomplete."),
    ):
        store.save_announcement_feed_item(
            AnnouncementFeedItem(
                canonical_key=f"school-telehealth-{version}",
                title="Youth care access update",
                url=f"https://example.org/youth-care/{version}",
                source="rss",
                feed="rss",
                published_at=published_at,
                tags=["school telehealth", "mental health parity"],
                summary="Remote behavioral-health delivery and coverage policy update.",
                evidence=[
                    AnnouncementFeedEvidence(
                        kind="article",
                        title=f"Evidence {version}",
                        url=f"https://example.org/youth-care/{version}/source",
                        snippet=qualification,
                        metadata={"version": version},
                    )
                ],
            )
        )

    paraphrased = retrieve_rss_announcement_history_impl(
        query="school telehealth parity",
        database_url=database_url,
    )
    ambiguous_title = retrieve_rss_announcement_history_impl(
        query="Youth care access update",
        database_url=database_url,
    )
    unrelated = retrieve_rss_announcement_history_impl(
        query="quantum geology",
        database_url=database_url,
    )

    assert {item["feed_item_id"] for item in paraphrased["items"]} == {
        "school-telehealth-v1",
        "school-telehealth-v2",
    }
    assert {item["feed_item_id"] for item in ambiguous_title["items"]} == {
        "school-telehealth-v1",
        "school-telehealth-v2",
    }
    assert {item["published_at"] for item in ambiguous_title["items"]} == {
        "2026-08-01",
        "2026-09-01",
    }
    assert (
        len(
            {
                item["source_snapshot"]["sha256"]
                for item in ambiguous_title["items"]
            }
        )
        == 2
    )
    assert unrelated["items"] == []


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


def test_linked_preprint_query_ranks_whole_term_matches_and_reports_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))
    before = discovery_path.read_bytes()

    expected_ids = ["synthetic:preprint:latest", "synthetic:preprint:older"]
    for query in ("depression neuromodulation", "NEUROMODULATION, depression!"):
        items, diagnostics = announcement_tools._linked_preprint_discovery_items(
            query=query,
            limit=8,
        )
        assert [item["feed_item_id"] for item in items] == expected_ids
        assert diagnostics[0]["key"] == "linked_discovery_store"

    hyphenated, _ = announcement_tools._linked_preprint_discovery_items(
        query="depression clinical-ai",
        limit=8,
    )
    assert [item["feed_item_id"] for item in hyphenated] == [
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    ]

    partial, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query="depression unknownterm",
        limit=8,
    )
    assert [item["feed_item_id"] for item in partial] == expected_ids
    diagnostic_map = {item["key"]: item for item in diagnostics}
    assert diagnostic_map["linked_discovery_query_policy"]["value"] == (
        "ranked_whole_token_any"
    )
    assert diagnostic_map["linked_discovery_query_coverage"]["value"] == (
        "matched=depression; unmatched=unknownterm"
    )
    assert discovery_path.read_bytes() == before


@pytest.mark.parametrize(
    "query",
    [
        (
            "Which recent saved preprint should Example Institute review "
            "for depression neuromodulation?"
        ),
        "neuromodulation and depression evidence for an internal review",
        "DEPRESSION / neuromodulation -- relevance assessment",
    ],
)
def test_linked_preprint_query_keeps_topical_recall_with_task_wording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    items, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query=query,
        limit=8,
    )

    assert {item["feed_item_id"] for item in items} == {
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    }
    assert any(
        item["key"] == "linked_discovery_inventory" and item["value"] == "3"
        for item in diagnostics
    )


@pytest.mark.parametrize(
    ("query", "expected_id"),
    [
        ("id:synthetic:preprint:sparse", "synthetic:preprint:sparse"),
        ("10.0000/synthetic-001", "synthetic:preprint:latest"),
        ("date:2026-08-02", "synthetic:preprint:older"),
    ],
)
def test_linked_preprint_query_preserves_explicit_identity_and_field_constraints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_id: str,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    items, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query=query,
        limit=8,
    )

    assert [item["feed_item_id"] for item in items] == [expected_id]
    assert any(
        item["key"] == "linked_discovery_explicit_filters"
        and item["value"] == "1"
        for item in diagnostics
    )


def test_linked_preprint_query_distinguishes_natural_date_context_from_typed_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    natural, natural_diagnostics = announcement_tools._linked_preprint_discovery_items(
        query="depression after 2026-08-01",
        limit=8,
    )
    typed_after, _ = announcement_tools._linked_preprint_discovery_items(
        query="depression after:2026-08-01",
        limit=8,
    )
    exact_day, _ = announcement_tools._linked_preprint_discovery_items(
        query="date:2026-08-02",
        limit=8,
    )
    unsupported_only, unsupported_only_diagnostics = (
        announcement_tools._linked_preprint_discovery_items(
            query="after 2026-08-01",
            limit=8,
        )
    )

    expected_depression = [
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    ]
    assert [item["feed_item_id"] for item in natural] == expected_depression
    assert [item["feed_item_id"] for item in typed_after] == expected_depression
    assert [item["feed_item_id"] for item in exact_day] == [
        "synthetic:preprint:older"
    ]
    assert unsupported_only == []
    assert unsupported_only_diagnostics[0]["value"] == "unsupported_filter_syntax"
    diagnostic_map = {item["key"]: item for item in natural_diagnostics}
    assert diagnostic_map["linked_discovery_unsupported_filters"]["value"] == "1"
    assert "not silently enforced as exact-day" in diagnostic_map[
        "linked_discovery_unsupported_filters"
    ]["note"]


def test_linked_preprint_query_parses_quoted_multiword_source_and_escaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    with sqlite3.connect(discovery_path) as connection:
        connection.execute(
            "UPDATE discovery_candidates SET source = ? WHERE candidate_id = ?",
            ('Example "Research" Preprints', "synthetic:preprint:latest"),
        )
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    multiword, _ = announcement_tools._linked_preprint_discovery_items(
        query='depression source:"Example Preprints"',
        limit=8,
    )
    escaped, _ = announcement_tools._linked_preprint_discovery_items(
        query='depression source:"Example \\"Research\\" Preprints"',
        limit=8,
    )
    mismatch, _ = announcement_tools._linked_preprint_discovery_items(
        query='depression source:"Other Institute"',
        limit=8,
    )

    assert [item["feed_item_id"] for item in multiword] == [
        "synthetic:preprint:older"
    ]
    assert [item["feed_item_id"] for item in escaped] == [
        "synthetic:preprint:latest"
    ]
    assert mismatch == []


@pytest.mark.parametrize(
    "query",
    [
        'depression source:"Example Preprints',
        "depression source:",
    ],
)
def test_linked_preprint_query_reports_malformed_explicit_filter_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    items, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query=query,
        limit=8,
    )

    assert items == []
    assert diagnostics == [
        {
            "key": "linked_discovery_query",
            "value": "malformed_explicit_filter",
            "note": (
                "An explicit filter had an empty or unclosed quoted value. "
                "No saved-history rows were read; correct the filter syntax."
            ),
        }
    ]


def test_history_authority_constraints_normalize_equivalent_date_and_source_syntax() -> None:
    natural_after = announcement_tools.linked_history_authority_constraints(
        "depression after 2026-08-01"
    )
    typed_after = announcement_tools.linked_history_authority_constraints(
        "depression after:2026-08-01"
    )
    quoted_source = announcement_tools.linked_history_authority_constraints(
        'depression source:"Example Institute"'
    )
    bare_candidate_id = announcement_tools.linked_history_authority_constraints(
        "source:arxiv:260722776v1"
    )
    typed_candidate_id = announcement_tools.linked_history_authority_constraints(
        "id:source:arxiv:260722776v1"
    )
    candidate_alias = announcement_tools.linked_history_authority_constraints(
        "candidate:synthetic-preprint-a"
    )
    id_alias = announcement_tools.linked_history_authority_constraints(
        "id:synthetic-preprint-a"
    )
    resolver_doi = announcement_tools.linked_history_authority_constraints(
        "Review https://doi.org/10.1234/example.42"
    )
    typed_doi = announcement_tools.linked_history_authority_constraints(
        "doi:10.1234/example.42"
    )

    assert natural_after == typed_after == ("date:after:2026-08-01",)
    assert quoted_source == ("source:exact:example institute",)
    assert bare_candidate_id == typed_candidate_id == (
        "candidate_id:exact:source:arxiv:260722776v1",
    )
    assert candidate_alias == id_alias == (
        "candidate_id:exact:synthetic-preprint-a",
    )
    assert resolver_doi == typed_doi == ("doi:exact:10.1234/example.42",)


def test_history_authority_constraints_do_not_promote_incidental_date_or_url() -> None:
    constraints = announcement_tools.linked_history_authority_constraint_records(
        "Compare the 2026-08-01 archive note at https://example.test/context"
    )

    assert constraints == ()


def test_linked_preprint_query_keeps_honest_empty_and_generic_word_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    unrelated, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query="quantum geology",
        limit=8,
    )
    generic, generic_diagnostics = announcement_tools._linked_preprint_discovery_items(
        query="the and or for",
        limit=8,
    )

    assert unrelated == []
    assert any(
        item["key"] == "linked_discovery_inventory" and item["value"] == "3"
        for item in diagnostics
    )
    assert generic == []
    assert generic_diagnostics == [
        {
            "key": "linked_discovery_query",
            "value": "no_search_terms",
            "note": (
                "The linked preprint query contained no bounded topical terms or "
                "explicit filters."
            ),
        }
    ]


def test_linked_preprint_query_matches_term_boundaries_before_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    with sqlite3.connect(discovery_path) as connection:
        connection.executemany(
            "INSERT INTO discovery_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "synthetic:preprint:brain-only",
                    "preprint",
                    "Example Preprints",
                    "synthetic-boundary-001",
                    "Clinical brain imaging",
                    "Structural evaluation of synthetic mood outcomes.",
                    "2026-09-15",
                    "https://example.test/preprints/synthetic-boundary-001",
                    json.dumps(["mood"]),
                    "candidate",
                    "2026-09-15T12:00:00Z",
                    "{}",
                ),
                (
                    "synthetic:preprint:standalone-ai",
                    "preprint",
                    "Example Preprints",
                    "synthetic-boundary-002",
                    "Clinical AI evaluation",
                    "A synthetic algorithm evaluation.",
                    "2026-09-14",
                    "https://example.test/preprints/synthetic-boundary-002",
                    json.dumps(["evaluation"]),
                    "candidate",
                    "2026-09-14T12:00:00Z",
                    "{}",
                ),
                (
                    "synthetic:preprint:punctuated-ai",
                    "preprint",
                    "Example Preprints",
                    "synthetic-boundary-003",
                    "Clinical / AI: implementation",
                    "A synthetic workflow report.",
                    "2026-09-13",
                    "https://example.test/preprints/synthetic-boundary-003",
                    json.dumps(["implementation"]),
                    "candidate",
                    "2026-09-13T12:00:00Z",
                    "{}",
                ),
                (
                    "synthetic:preprint:industrial-only",
                    "preprint",
                    "Example Preprints",
                    "synthetic-boundary-004",
                    "Industrial workflow review",
                    "A synthetic operations report.",
                    "2026-09-12",
                    "https://example.test/preprints/synthetic-boundary-004",
                    json.dumps(["operations"]),
                    "candidate",
                    "2026-09-12T12:00:00Z",
                    "{}",
                ),
                (
                    "synthetic:preprint:standalone-trial",
                    "preprint",
                    "Example Preprints",
                    "synthetic-boundary-005",
                    "Trial workflow review",
                    "A synthetic protocol report.",
                    "2026-09-11",
                    "https://example.test/preprints/synthetic-boundary-005",
                    json.dumps(["protocol"]),
                    "candidate",
                    "2026-09-11T12:00:00Z",
                    "{}",
                ),
            ],
        )
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url)
    canonical_path = tmp_path / "announcement-context.db"
    discovery_before = discovery_path.read_bytes()
    canonical_before = canonical_path.read_bytes()

    expected_ai_ids = [
        "synthetic:preprint:standalone-ai",
        "synthetic:preprint:punctuated-ai",
        "synthetic:preprint:latest",
        "synthetic:preprint:brain-only",
    ]
    for query in ("clinical AI", "AI / clinical"):
        matches, _ = announcement_tools._linked_preprint_discovery_items(
            query=query,
            limit=8,
        )
        assert [item["feed_item_id"] for item in matches] == expected_ai_ids

    limited = retrieve_preprint_announcement_history_impl(
        "clinical AI",
        limit=1,
        database_url=database_url,
    )
    assert [item["feed_item_id"] for item in limited["items"]] == [
        "synthetic:preprint:standalone-ai"
    ]

    other_boundary, _ = announcement_tools._linked_preprint_discovery_items(
        query="trial",
        limit=1,
    )
    assert [item["feed_item_id"] for item in other_boundary] == [
        "synthetic:preprint:standalone-trial"
    ]
    assert discovery_path.read_bytes() == discovery_before
    assert canonical_path.read_bytes() == canonical_before


def test_linked_preprint_query_preserves_single_empty_limit_and_sparse_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    single_term, _ = announcement_tools._linked_preprint_discovery_items(
        query="depression",
        limit=8,
    )
    assert [item["feed_item_id"] for item in single_term] == [
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    ]

    recent, _ = announcement_tools._linked_preprint_discovery_items(query="", limit=1)
    assert [item["feed_item_id"] for item in recent] == ["synthetic:preprint:latest"]

    sparse, _ = announcement_tools._linked_preprint_discovery_items(
        query="sparse",
        limit=8,
    )
    assert [item["feed_item_id"] for item in sparse] == [
        "synthetic:preprint:sparse"
    ]
    assert sparse[0]["summary"] == ""
    assert sparse[0]["tags"] == []


def test_linked_preprint_query_does_not_treat_punctuation_as_wildcards_or_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))

    wildcard_items, _ = announcement_tools._linked_preprint_discovery_items(
        query="%_",
        limit=8,
    )
    sql_like_items, _ = announcement_tools._linked_preprint_discovery_items(
        query="depression' OR 1=1 --",
        limit=8,
    )
    assert wildcard_items == []
    assert [item["feed_item_id"] for item in sql_like_items] == [
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    ]

    too_many_terms = " ".join(
        f"term{index}"
        for index in range(announcement_tools.MAX_LINKED_QUERY_TERMS + 1)
    )
    items, diagnostics = announcement_tools._linked_preprint_discovery_items(
        query=too_many_terms,
        limit=8,
    )
    assert items == []
    assert diagnostics == [
        {
            "key": "linked_discovery_query",
            "value": "too_many_terms",
            "note": (
                "The linked preprint query exceeded the bounded term limit; "
                "no requested terms were dropped."
            ),
        }
    ]


def test_preprint_wrapper_uses_cross_field_terms_without_changing_temp_stores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url)
    canonical_path = tmp_path / "announcement-context.db"
    discovery_before = discovery_path.read_bytes()
    canonical_before = canonical_path.read_bytes()

    result = retrieve_preprint_announcement_history_impl(
        "depression neuromodulation",
        limit=1,
        database_url=database_url,
    )

    assert result["status"] == "success"
    assert result["item_count"] == 1
    assert result["items"][0]["feed_item_id"] == "synthetic:preprint:latest"
    assert result["items"][0]["evidence_status"] == "discovery_candidate"
    assert discovery_path.read_bytes() == discovery_before
    assert canonical_path.read_bytes() == canonical_before


def test_preprint_wrapper_recovers_duo066_natural_query_without_empty_store_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_path = tmp_path / "discovery.sqlite"
    _synthetic_linked_discovery_store(discovery_path)
    monkeypatch.setenv("DISCOVERY_STORE_PATH", str(discovery_path))
    database_url = _database_url(tmp_path)

    recovered = retrieve_preprint_announcement_history_impl(
        (
            "depression neuromodulation recent saved preprint review "
            "Example Institute"
        ),
        database_url=database_url,
    )
    unrelated = retrieve_preprint_announcement_history_impl(
        "quantum geology",
        database_url=database_url,
    )

    assert [item["feed_item_id"] for item in recovered["items"]] == [
        "synthetic:preprint:latest",
        "synthetic:preprint:older",
    ]
    assert recovered["item_count"] == 2
    assert unrelated["items"] == []
    assert unrelated["blockers"] == [
        "No matching announcement feed history was found."
    ]


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
