from __future__ import annotations

import zipfile
from pathlib import Path

from keystone_agents.presentation_index import (
    presentation_hits_to_artifact_refs,
    refresh_presentation_index,
    search_presentation_index,
)


def _write_deck(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Clinical AI</a:t>'
            '<a:t>Evidence validation framework</a:t></p:sld>',
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<p:notes xmlns:p="p" xmlns:a="a"><a:t>Review implementation risk</a:t></p:notes>',
        )
        archive.writestr(
            "ppt/slides/slide2.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Deployment</a:t>'
            '<a:t>Workflow monitoring</a:t></p:sld>',
        )


def test_index_searches_slide_text_and_notes_then_promotes_typed_refs(
    tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "library"
    _write_deck(library / "Clinical AI" / "reviewed-deck.pptx")
    monkeypatch.setenv("KEYSTONE_PRESENTATION_LIBRARY_ROOT", str(library))
    database = tmp_path / "presentation-index.sqlite3"

    receipt = refresh_presentation_index(database, live=True)
    hits = search_presentation_index(database, "clinical evidence risk")
    refs = presentation_hits_to_artifact_refs(hits)

    assert receipt["status"] == "success"
    assert receipt["deck_count"] == 1
    assert receipt["slide_count"] == 2
    assert len(hits) == 1
    assert hits[0]["relative_path"] == "Clinical AI/reviewed-deck.pptx"
    assert hits[0]["slide_number"] == 1
    assert "implementation risk" in hits[0]["evidence_excerpt"].lower()
    assert refs[0].artifact_type == "presentation_slide_evidence"
    assert refs[0].approval_state == "approved_for_internal_context"
    assert refs[0].metadata["relative_path"] == "Clinical AI/reviewed-deck.pptx"
    assert refs[0].metadata["snapshot"] is True
    assert refs[0].metadata["parent_modified"] is False
    assert refs[0].metadata["send_enabled"] is False
    assert refs[0].metadata["controlled_vocabulary_version"] == (
        "keystone.workflow_vocabulary.v1"
    )
    assert "kba:object:presentation_slide" in refs[0].metadata["controlled_tags"]
    assert "kba:safety:read_only" in refs[0].metadata["controlled_tags"]


def test_index_dry_run_and_missing_query_are_side_effect_free(tmp_path: Path) -> None:
    database = tmp_path / "presentation-index.sqlite3"

    receipt = refresh_presentation_index(database, live=False)

    assert receipt["status"] == "dry-run"
    assert receipt["parent_modified"] is False
    assert database.exists() is False
    assert search_presentation_index(database, "") == []
