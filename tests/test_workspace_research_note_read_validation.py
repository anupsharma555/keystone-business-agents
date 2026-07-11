from __future__ import annotations

from scripts.run_workspace_research_note_read_validation import (
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    execute_research_note_read,
)


def test_research_note_read_resolves_latest_unique_doc_without_body_persistence() -> None:
    result = execute_research_note_read(
        live=True,
        search_files=lambda *_args, **_kwargs: {
            "items": [
                {"id": "folder-1", "name": "Research", "mime_type": GOOGLE_FOLDER_MIME}
            ]
        },
        list_folder=lambda *_args, **_kwargs: {
            "items": [
                {
                    "id": "doc-old",
                    "name": "Old note",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-05-01T00:00:00Z",
                },
                {
                    "id": "doc-new",
                    "name": "Latest note",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-05-20T00:00:00Z",
                },
            ]
        },
        read_doc=lambda document_id, **_kwargs: {
            "status": "success",
            "document_id": document_id,
            "title": "Latest note",
            "text": "Bounded research note body.",
            "char_count": 27,
            "truncated": False,
        },
    )

    assert result["status"] == "pass"
    assert result["document_matches"] == 2
    assert result["char_count"] == 27
    assert result["body_persisted"] is False
    assert "Bounded research note body" not in str(result)
    assert result["provider_reads"] == 3
    assert result["provider_writes"] == 0


def test_research_note_read_blocks_ambiguous_exact_folder() -> None:
    result = execute_research_note_read(
        live=True,
        search_files=lambda *_args, **_kwargs: {
            "items": [
                {"id": "folder-1", "name": "Research", "mime_type": GOOGLE_FOLDER_MIME},
                {"id": "folder-2", "name": "research", "mime_type": GOOGLE_FOLDER_MIME},
            ]
        },
    )

    assert result["status"] == "blocked"
    assert result["folder_matches"] == 2
    assert result["provider_writes"] == 0


def test_research_note_read_blocks_tied_latest_documents() -> None:
    result = execute_research_note_read(
        live=True,
        search_files=lambda *_args, **_kwargs: {
            "items": [
                {"id": "folder-1", "name": "Research", "mime_type": GOOGLE_FOLDER_MIME}
            ]
        },
        list_folder=lambda *_args, **_kwargs: {
            "items": [
                {
                    "id": "doc-1",
                    "name": "One",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-05-20T00:00:00Z",
                },
                {
                    "id": "doc-2",
                    "name": "Two",
                    "mime_type": GOOGLE_DOC_MIME,
                    "modified_time": "2026-05-20T00:00:00Z",
                },
            ]
        },
    )

    assert result["status"] == "blocked"
    assert result["document_matches"] == 2
    assert result["provider_writes"] == 0


def test_research_note_read_dry_run_uses_no_provider() -> None:
    result = execute_research_note_read(live=False)

    assert result["status"] == "dry-run"
    assert result["provider_reads"] == 0
    assert result["provider_writes"] == 0
