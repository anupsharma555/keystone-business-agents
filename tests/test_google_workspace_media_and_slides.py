from __future__ import annotations

from typing import Any

import pytest

from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.tools import internal_data_tools


class _Request:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def execute(self) -> Any:
        return self.payload


class _MediaFiles:
    def get(self, **_kwargs: object) -> _Request:
        return _Request(
            {
                "id": "mediaKBA1",
                "name": "scanned-brief.pdf",
                "mimeType": "application/pdf",
                "webViewLink": "https://drive.google.com/file/d/mediaKBA1/view",
                "modifiedTime": "2026-08-02T00:00:00Z",
                "size": "2048",
                "parents": ["folderKBA1"],
                "trashed": False,
            }
        )

    def get_media(self, **_kwargs: object) -> _Request:
        return _Request(b"%PDF-1.4 bounded fixture")


class _MediaDrive:
    def files(self) -> _MediaFiles:
        return _MediaFiles()


def test_workspace_media_and_slide_writes_are_safe_dry_runs() -> None:
    media = internal_data_tools.google_drive_media_ocr_read_impl("mediaKBA1")
    slides = internal_data_tools.google_slide_deck_write_impl(
        "Evaluation brief",
        [{"title": "Evidence", "body": "Observed and planned findings."}],
    )

    assert media["status"] == "dry-run"
    assert media["provider_bytes_downloaded"] is False
    assert slides["status"] == "dry-run"
    assert slides["provider_mutated"] is False
    assert slides["slide_count"] == 1


def test_workspace_media_and_slides_publish_bounded_function_schemas() -> None:
    agent = build_google_workspace_context_agent()
    tools = {tool.name: tool for tool in agent.tools}
    ocr_schema = tools["google_drive_media_ocr_read"].params_json_schema["properties"]
    slides_schema = tools["google_slide_deck_write"].params_json_schema["properties"]

    assert ocr_schema["max_bytes"]["maximum"] == 25_000_000
    assert ocr_schema["max_pages"]["maximum"] == 20
    assert ocr_schema["max_chars"]["maximum"] == 30_000
    assert slides_schema["title"]["maxLength"] == 300
    assert slides_schema["slides_json"]["maxLength"] == 260_000
    assert slides_schema["content_mode"]["enum"] == ["replace", "append"]


def test_drive_media_read_verifies_scope_and_returns_bounded_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _MediaDrive()},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKBA1",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_under_folder",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_extract_drive_media_text",
        lambda *_args, **_kwargs: ("A" * 2000, "tesseract", "", 2),
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_download_drive_media_bounded",
        lambda *_args, **_kwargs: b"%PDF-1.4 bounded fixture",
    )

    result = internal_data_tools.google_drive_media_ocr_read_impl(
        "mediaKBA1",
        max_chars=1000,
        live=True,
    )

    assert result["status"] == "partial"
    assert result["source_identity_verified"] is True
    assert result["provider_bytes_downloaded"] is True
    assert result["extraction_method"] == "tesseract"
    assert result["pages_processed"] == 2
    assert result["char_count"] == 1000
    assert result["truncated"] is True
    assert result["next_request"]["start_char"] == 1000
    assert result["next_request"]["expected_content_sha256"] == result["content_sha256"]
    assert len(result["content_sha256"]) == 64
    assert "bytes" not in result


def test_drive_media_read_blocks_declared_image_pixel_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ImageFiles(_MediaFiles):
        def get(self, **_kwargs: object) -> _Request:
            return _Request(
                {
                    "id": "mediaKBA1",
                    "name": "oversized.png",
                    "mimeType": "image/png",
                    "size": "2048",
                    "imageMediaMetadata": {"width": 20_000, "height": 20_000},
                    "parents": ["folderKBA1"],
                    "trashed": False,
                }
            )

    class _ImageDrive:
        def files(self) -> _ImageFiles:
            return _ImageFiles()

    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _ImageDrive()},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKBA1",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_under_folder",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_download_drive_media_bounded",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized image must be blocked before download")
        ),
    )

    with pytest.raises(RuntimeError, match="OCR pixel limit"):
        internal_data_tools.google_drive_media_ocr_read_impl(
            "mediaKBA1",
            live=True,
        )


class _SlidesState:
    def __init__(self) -> None:
        self.presentation = {
            "presentationId": "deckKBA1",
            "revisionId": "rev-1",
            "slides": [{"objectId": "initialSlide", "pageElements": []}],
        }


class _SlidesPresentations:
    def __init__(self, state: _SlidesState) -> None:
        self.state = state

    def create(self, **_kwargs: object) -> _Request:
        return _Request({"presentationId": "deckKBA1"})

    def get(self, **_kwargs: object) -> _Request:
        return _Request(self.state.presentation)

    def batchUpdate(self, *, body: dict[str, Any], **_kwargs: object) -> _Request:
        write_control = body.get("writeControl", {})
        if write_control:
            assert write_control["requiredRevisionId"] == self.state.presentation["revisionId"]
        slide_by_id = {
            str(slide["objectId"]): slide for slide in self.state.presentation["slides"]
        }
        shape_page: dict[str, str] = {}
        shape_by_id: dict[str, dict[str, Any]] = {}
        for request in body["requests"]:
            if "createSlide" in request:
                payload = request["createSlide"]
                slide = {"objectId": payload["objectId"], "pageElements": []}
                index = int(payload.get("insertionIndex", len(slide_by_id)))
                self.state.presentation["slides"].insert(index, slide)
                slide_by_id[payload["objectId"]] = slide
            elif "createShape" in request:
                payload = request["createShape"]
                shape_id = payload["objectId"]
                page_id = payload["elementProperties"]["pageObjectId"]
                shape = {
                    "objectId": shape_id,
                    "shape": {"text": {"textElements": []}},
                }
                slide_by_id[page_id]["pageElements"].append(shape)
                shape_page[shape_id] = page_id
                shape_by_id[shape_id] = shape
            elif "insertText" in request:
                payload = request["insertText"]
                shape_by_id[payload["objectId"]]["shape"]["text"]["textElements"] = [
                    {"textRun": {"content": payload["text"]}}
                ]
            elif "deleteObject" in request:
                object_id = request["deleteObject"]["objectId"]
                self.state.presentation["slides"] = [
                    slide
                    for slide in self.state.presentation["slides"]
                    if slide["objectId"] != object_id
                ]
        revision_number = int(str(self.state.presentation["revisionId"]).split("-")[-1]) + 1
        self.state.presentation["revisionId"] = f"rev-{revision_number}"
        return _Request(
            {
                "replies": [],
                "writeControl": {
                    "requiredRevisionId": self.state.presentation["revisionId"]
                },
            }
        )


class _SlidesService:
    def __init__(self, state: _SlidesState) -> None:
        self.state = state

    def presentations(self) -> _SlidesPresentations:
        return _SlidesPresentations(self.state)


class _SlideDriveFiles:
    def __init__(self, *, name: str = "Evaluation brief") -> None:
        self.updates: list[dict[str, object]] = []
        self.name = name
        self.trashed = False

    def get(self, **_kwargs: object) -> _Request:
        return _Request(
            {
                "id": "deckKBA1",
                "name": self.name,
                "mimeType": internal_data_tools.GOOGLE_SLIDES_MIME_TYPE,
                "trashed": self.trashed,
                "parents": ["folderKBA1"],
            }
        )

    def update(self, **_kwargs: object) -> _Request:
        self.updates.append(dict(_kwargs))
        body = _kwargs.get("body", {})
        if isinstance(body, dict):
            if "name" in body:
                self.name = str(body["name"])
            if "trashed" in body:
                self.trashed = bool(body["trashed"])
        return _Request(
            {
                "id": "deckKBA1",
                "name": self.name,
                "mimeType": internal_data_tools.GOOGLE_SLIDES_MIME_TYPE,
                "trashed": self.trashed,
            }
        )


class _SlideDrive:
    def __init__(self, *, name: str = "Evaluation brief") -> None:
        self.file_service = _SlideDriveFiles(name=name)

    def files(self) -> _SlideDriveFiles:
        return self.file_service


def test_google_slides_create_batches_content_and_verifies_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _SlideDrive(), "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKBA1",
    )
    monkeypatch.setattr(internal_data_tools, "_move_drive_file_to_folder", lambda *_: None)

    result = internal_data_tools.google_slide_deck_write_impl(
        "Evaluation brief",
        [
            {"title": "Evidence", "body": "Observed findings"},
            {"title": "Next test", "body": "Planned validation"},
        ],
        approval_reference="approval-KBA1",
        live=True,
    )

    assert result["status"] == "success"
    assert result["created"] is True
    assert result["inserted_slide_count"] == 2
    assert result["replaced_slide_count"] == 1
    assert result["verification"]["slide_content_match"] is True
    assert result["verification"]["optimistic_concurrency_used"] is True
    assert [slide["objectId"] for slide in state.presentation["slides"]] == result[
        "inserted_slide_ids"
    ]


def test_google_slides_live_write_requires_both_approval_and_write_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_WORKSPACE_WRITES_ENABLED", raising=False)

    with pytest.raises(RuntimeError, match="non-empty approval_reference"):
        internal_data_tools.google_slide_deck_write_impl(
            "Deck",
            [{"title": "Only", "body": "Slide"}],
            live=True,
        )

    with pytest.raises(RuntimeError, match="writes are disabled"):
        internal_data_tools.google_slide_deck_write_impl(
            "Deck",
            [{"title": "Only", "body": "Slide"}],
            approval_reference="approval-KBA1",
            live=True,
        )


def test_google_slides_create_compensates_if_folder_move_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    drive = _SlideDrive()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": drive, "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKBA1",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_move_drive_file_to_folder",
        lambda *_: (_ for _ in ()).throw(RuntimeError("move failed")),
    )

    with pytest.raises(RuntimeError, match="move failed"):
        internal_data_tools.google_slide_deck_write_impl(
            "Deck",
            [{"title": "Only", "body": "Slide"}],
            approval_reference="approval-KBA1",
            live=True,
        )

    assert drive.file_service.updates == [
        {
            "fileId": "deckKBA1",
            "body": {"trashed": True},
            "fields": "id,trashed",
        }
    ]


def test_google_slides_create_surfaces_failed_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TrashFailFiles(_SlideDriveFiles):
        def update(self, **kwargs: object) -> _Request:
            body = kwargs.get("body", {})
            if isinstance(body, dict) and body.get("trashed") is True:
                raise RuntimeError("trash failed")
            return super().update(**kwargs)

    class _TrashFailDrive(_SlideDrive):
        def __init__(self) -> None:
            self.file_service = _TrashFailFiles(name="Deck")

    state = _SlidesState()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _TrashFailDrive(), "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKBA1",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_move_drive_file_to_folder",
        lambda *_: (_ for _ in ()).throw(RuntimeError("move failed")),
    )

    with pytest.raises(RuntimeError, match="Compensation incomplete.*trash failed"):
        internal_data_tools.google_slide_deck_write_impl(
            "Deck",
            [{"title": "Only", "body": "Slide"}],
            approval_reference="approval-KBA1",
            live=True,
        )


def test_google_slides_create_blocks_when_approved_folder_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _SlideDrive(), "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_find_drive_folder_path", lambda *_: "")

    with pytest.raises(RuntimeError, match="destination folder does not exist"):
        internal_data_tools.google_slide_deck_write_impl(
            "Deck",
            [{"title": "Only", "body": "Slide"}],
            approval_reference="approval-KBA1",
            live=True,
        )

    assert state.presentation["slides"] == [
        {"objectId": "initialSlide", "pageElements": []}
    ]


def test_google_slides_existing_replace_stages_before_delete_and_restores_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    drive = _SlideDrive(name="Original title")
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": drive, "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_in_folder", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools,
        "_google_slides_artifacts",
        lambda *_args, **_kwargs: {"all_slides": []},
    )

    with pytest.raises(RuntimeError, match="did not verify the requested slide content"):
        internal_data_tools.google_slide_deck_write_impl(
            "Renamed title",
            [{"title": "New", "body": "Verified before replacement"}],
            presentation_id_or_url="deckKBA1",
            approval_reference="approval-KBA1",
            live=True,
        )

    assert [slide["objectId"] for slide in state.presentation["slides"]] == [
        "initialSlide"
    ]
    assert drive.file_service.name == "Original title"


def test_google_slides_existing_edit_updates_and_verifies_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    drive = _SlideDrive(name="Original title")
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": drive, "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_in_folder", lambda *_: None)

    result = internal_data_tools.google_slide_deck_write_impl(
        "Renamed title",
        [{"title": "New", "body": "Verified replacement"}],
        presentation_id_or_url="deckKBA1",
        approval_reference="approval-KBA1",
        live=True,
    )

    assert result["status"] == "success"
    assert result["verification"]["title_match"] is True
    assert drive.file_service.name == "Renamed title"
    assert [slide["objectId"] for slide in state.presentation["slides"]] == result[
        "inserted_slide_ids"
    ]


def test_google_slides_append_preserves_existing_slides_with_revision_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _SlidesState()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _SlideDrive(), "slides": _SlidesService(state)},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_in_folder", lambda *_: None)

    result = internal_data_tools.google_slide_deck_write_impl(
        "Evaluation brief",
        [{"title": "Appendix", "body": "Additional verified evidence"}],
        presentation_id_or_url="deckKBA1",
        content_mode="append",
        approval_reference="approval-KBA1",
        live=True,
    )

    assert result["status"] == "success"
    assert result["replaced_slide_count"] == 0
    assert result["verification"]["optimistic_concurrency_used"] is True
    assert [slide["objectId"] for slide in state.presentation["slides"]] == [
        "initialSlide",
        *result["inserted_slide_ids"],
    ]


def test_google_slides_revision_conflict_does_not_delete_existing_slides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RevisionConflictPresentations(_SlidesPresentations):
        def batchUpdate(self, *, body: dict[str, Any], **_kwargs: object) -> _Request:
            assert body["writeControl"]["requiredRevisionId"] == "rev-1"
            raise RuntimeError("provider revision mismatch")

    class _RevisionConflictSlides(_SlidesService):
        def presentations(self) -> _RevisionConflictPresentations:
            return _RevisionConflictPresentations(self.state)

    state = _SlidesState()
    monkeypatch.setenv("GOOGLE_WORKSPACE_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {
            "drive": _SlideDrive(),
            "slides": _RevisionConflictSlides(state),
        },
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_in_folder", lambda *_: None)

    with pytest.raises(RuntimeError, match="provider revision mismatch"):
        internal_data_tools.google_slide_deck_write_impl(
            "Evaluation brief",
            [{"title": "New", "body": "Should not replace concurrent work"}],
            presentation_id_or_url="deckKBA1",
            approval_reference="approval-KBA1",
            live=True,
        )

    assert [slide["objectId"] for slide in state.presentation["slides"]] == [
        "initialSlide"
    ]


def test_drive_media_download_aborts_when_stream_crosses_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ChunkDownloader:
        def __init__(self, buffer: Any) -> None:
            self.buffer = buffer
            self.chunks = [b"1234", b"5678"]

        def next_chunk(self, **_kwargs: object) -> tuple[None, bool]:
            self.buffer.write(self.chunks.pop(0))
            return None, not self.chunks

    monkeypatch.setattr(
        internal_data_tools,
        "_media_io_base_download",
        lambda buffer, _request, **_kwargs: _ChunkDownloader(buffer),
    )

    with pytest.raises(RuntimeError, match="6-byte download limit"):
        internal_data_tools._download_drive_media_bounded(
            _MediaDrive(),
            "mediaKBA1",
            max_bytes=6,
        )


def test_slide_request_builder_avoids_empty_insert_text_requests() -> None:
    requests, slide_ids = internal_data_tools._google_slide_write_requests(
        [{"title": "Title only", "body": ""}],
        insertion_index=2,
    )

    assert len(slide_ids) == 1
    assert requests[0]["createSlide"]["insertionIndex"] == 2
    assert [request["insertText"]["text"] for request in requests if "insertText" in request] == [
        "Title only"
    ]
