from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from keystone_agents.tools import internal_data_tools
from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_drive_get_file_metadata_impl,
    google_drive_list_folder_impl,
    google_drive_search_files_impl,
    google_sheet_read_table_impl,
    google_slide_deck_read_impl,
    presentation_read_local_impl,
    presentation_search_local_impl,
)


def test_workspace_read_dry_runs_name_operations_and_safe_counts() -> None:
    doc = google_doc_read_impl("docKBA1")
    listed = google_drive_list_folder_impl("KNIOps")
    searched = google_drive_search_files_impl("proposal", folder_path="KNIOps")
    metadata = google_drive_get_file_metadata_impl("fileKBA1", folder_path="KNIOps")
    sheet = google_sheet_read_table_impl("sheetKBA1", sheet_name="Validation")
    slides = google_slide_deck_read_impl("deckKBA1")

    assert doc["operation"] == "read_doc"
    assert listed["operation"] == "list_folder"
    assert listed["item_count"] == 0
    assert searched["operation"] == "search_files"
    assert searched["item_count"] == 0
    assert metadata["operation"] == "get_file_metadata"
    assert metadata["file_id"] == "fileKBA1"
    assert sheet["operation"] == "read_table"
    assert sheet["row_count"] == 0
    assert slides["operation"] == "read_slide_deck"
    assert slides["parent_modified"] is False


class _Request:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def execute(self) -> object:
        return self.payload


class _DriveFiles:
    def get(self, **_kwargs: object) -> _Request:
        return _Request(
            {
                "id": "deckKBA1",
                "name": "Clinical AI overview",
                "mimeType": internal_data_tools.GOOGLE_SLIDES_MIME_TYPE,
                "modifiedTime": "2026-07-11T00:00:00Z",
                "webViewLink": "https://docs.google.com/presentation/d/deckKBA1/edit",
                "parents": ["folderKBA1"],
                "trashed": False,
            }
        )


class _Drive:
    def files(self) -> _DriveFiles:
        return _DriveFiles()


class _Presentations:
    def get(self, **_kwargs: object) -> _Request:
        return _Request(
            {
                "presentationId": "deckKBA1",
                "slides": [
                    {
                        "objectId": "slideObject1",
                        "pageElements": [
                            {
                                "shape": {
                                    "placeholder": {"type": "TITLE"},
                                    "text": {
                                        "textElements": [
                                            {"textRun": {"content": "Evaluation Framework\n"}}
                                        ]
                                    },
                                }
                            },
                            {
                                "shape": {
                                    "text": {
                                        "textElements": [
                                            {"textRun": {"content": "Evidence and limitations"}}
                                        ]
                                    }
                                }
                            },
                        ],
                        "slideProperties": {
                            "notesPage": {
                                "pageElements": [
                                    {
                                        "shape": {
                                            "text": {
                                                "textElements": [
                                                    {"textRun": {"content": "Cite the source."}}
                                                ]
                                            }
                                        }
                                    }
                                ]
                            }
                        },
                    }
                ],
            }
        )


class _Slides:
    def presentations(self) -> _Presentations:
        return _Presentations()


def test_google_slide_read_preserves_provider_identity_and_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _Drive(), "slides": _Slides()},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_in_folder", lambda *_: None)

    result = google_slide_deck_read_impl("deckKBA1", live=True)

    assert result["status"] == "success"
    assert result["presentation_id"] == "deckKBA1"
    assert result["identity_scope"] == "provider_slide_object_id"
    assert result["parent_modified"] is False
    assert result["derived_copy_created"] is False
    assert result["slides"] == [
        {
            "slide_number": 1,
            "slide_id": "slideObject1",
            "title": "Evaluation Framework",
            "text": "Evaluation Framework\nEvidence and limitations",
            "speaker_notes": "Cite the source.",
            "text_truncated": False,
            "speaker_notes_truncated": False,
        }
    ]
    assert len(result["content_sha256"]) == 64


def test_powerpoint_open_xml_read_is_bounded_and_snapshot_identified() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Clinical AI</a:t><a:t>Evidence</a:t></p:sld>',
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<p:notes xmlns:p="p" xmlns:a="a"><a:t>Source note</a:t></p:notes>',
        )

    result = internal_data_tools._powerpoint_slide_artifacts(
        buffer.getvalue(),
        presentation_id="pptxKBA1",
        max_slides=10,
        max_chars=1000,
        include_speaker_notes=True,
    )

    assert result["all_slides"][0]["slide_id"] == "pptxKBA1:snapshot-slide:1"
    assert result["all_slides"][0]["title"] == "Clinical AI"
    assert result["all_slides"][0]["speaker_notes"] == "Source note"


def _write_test_deck(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Evaluation</a:t><a:t>Evidence</a:t></p:sld>',
        )
        archive.writestr(
            "ppt/notesSlides/notesSlide1.xml",
            '<p:notes xmlns:p="p" xmlns:a="a"><a:t>Review links</a:t></p:notes>',
        )


def test_local_presentation_search_and_read_preserve_relative_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deck = tmp_path / "Evaluation" / "clinical-ai-evaluation.pptx"
    _write_test_deck(deck)
    monkeypatch.setenv("KEYSTONE_PRESENTATION_LIBRARY_ROOT", str(tmp_path))

    search = presentation_search_local_impl("clinical evaluation", live=True)
    read = presentation_read_local_impl(
        "Evaluation/clinical-ai-evaluation.pptx", live=True
    )

    assert search["item_count"] == 1
    assert search["items"][0]["relative_path"] == (
        "Evaluation/clinical-ai-evaluation.pptx"
    )
    assert read["status"] == "success"
    assert read["relative_path"] == "Evaluation/clinical-ai-evaluation.pptx"
    assert read["slides"][0]["title"] == "Evaluation"
    assert read["slides"][0]["speaker_notes"] == "Review links"
    assert read["parent_modified"] is False
    assert len(read["content_sha256"]) == 64


def test_local_presentation_read_blocks_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEYSTONE_PRESENTATION_LIBRARY_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError, match="inside the configured library root"):
        presentation_read_local_impl("../outside.pptx", live=True)
