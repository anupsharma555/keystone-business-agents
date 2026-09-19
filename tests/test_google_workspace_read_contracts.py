from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from keystone_agents.tools import internal_data_tools
from keystone_agents.tools.internal_data_tools import (
    GoogleWorkspaceScopeError,
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
    assert "owners.displayName" in metadata["metadata_fields"]
    assert sheet["operation"] == "read_table"
    assert sheet["row_count"] == 0
    assert slides["operation"] == "read_slide_deck"
    assert slides["parent_modified"] is False


def test_sheet_read_requires_explicit_workbook_and_tab_identity() -> None:
    with pytest.raises(ValueError, match="spreadsheet_id_or_url"):
        google_sheet_read_table_impl(sheet_name="Validation")
    with pytest.raises(ValueError, match="sheet_name or range_a1"):
        google_sheet_read_table_impl("sheetKBA1")


def test_read_scope_validation_never_creates_a_missing_drive_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(internal_data_tools, "_find_drive_folder_path", lambda *_: "")

    with pytest.raises(RuntimeError, match="read-only validation will not create"):
        internal_data_tools._assert_drive_file_in_folder(
            object(),
            "fileKBA1",
            "KNIOps / Missing",
        )


def test_duplicate_drive_folder_name_is_ambiguous() -> None:
    class _DuplicateFolderFiles:
        def list(self, **_kwargs: object) -> _Request:
            return _Request(
                {
                    "files": [
                        {"id": "folder-one", "name": "Reports"},
                        {"id": "folder-two", "name": "Reports"},
                    ]
                }
            )

    class _DuplicateFolderDrive:
        def files(self) -> _DuplicateFolderFiles:
            return _DuplicateFolderFiles()

    with pytest.raises(RuntimeError, match="Multiple Google Drive folders"):
        internal_data_tools._find_drive_child_folder(
            _DuplicateFolderDrive(),
            "parent-id",
            "Reports",
        )


def test_drive_metadata_returns_bounded_owner_names_without_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _MetadataFiles:
        def get(self, **kwargs: object) -> _Request:
            captured.update(kwargs)
            return _Request(
                {
                    "id": "fileKBA1",
                    "name": "Evaluation.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-08-02T15:00:00Z",
                    "parents": ["folderKBA1"],
                    "trashed": False,
                    "owners": [
                        {
                            "displayName": "KNI Operator",
                            "emailAddress": "private@example.com",
                            "me": True,
                        }
                    ],
                }
            )

    class _MetadataDrive:
        def files(self) -> _MetadataFiles:
            return _MetadataFiles()

    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _MetadataDrive()},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(internal_data_tools, "_find_drive_folder_path", lambda *_: "folderKBA1")
    monkeypatch.setattr(internal_data_tools, "_assert_drive_file_under_folder", lambda *_: None)

    result = google_drive_get_file_metadata_impl("fileKBA1", live=True)

    assert "owners(displayName,me)" in str(captured["fields"])
    assert result["file"]["owners"] == [{"display_name": "KNI Operator", "me": True}]
    assert "private@example.com" not in str(result)


def test_workspace_services_build_only_the_authorized_selected_service(
    tmp_path: Path,
) -> None:
    drive_scope = "https://www.googleapis.com/auth/drive"
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({"scopes": [drive_scope]}), encoding="utf-8")
    built: list[tuple[str, str]] = []

    class _Credentials:
        expired = True
        valid = False
        refresh_token = "refresh"
        scopes = [drive_scope]
        granted_scopes = None

        def refresh(self, _request: object) -> None:
            self.expired = False
            self.valid = True

        def to_json(self) -> str:
            return json.dumps({"scopes": [drive_scope]})

    bundle = internal_data_tools._LazyGoogleWorkspaceServices(
        credentials=_Credentials(),
        build_service=lambda name, version, **_kwargs: built.append((name, version)) or object(),
        token_path=token_path,
        refresh_request=object,
    )

    assert bundle["drive"] is bundle["drive"]
    assert built == [("drive", "v3")]
    with pytest.raises(GoogleWorkspaceScopeError, match="slides service"):
        bundle["slides"]
    assert built == [("drive", "v3")]


def test_workspace_read_tool_returns_nonretryable_scope_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise GoogleWorkspaceScopeError(
            "drive",
            "https://www.googleapis.com/auth/drive",
        )

    monkeypatch.setattr(internal_data_tools, "google_drive_search_files_impl", blocked)

    payload = json.loads(
        internal_data_tools.google_drive_search_files(
            query="",
            mime_type="application/pdf",
            live=True,
        )
    )

    assert payload["status"] == "blocked"
    assert payload["error_code"] == "google_workspace_scope_missing"
    assert payload["retryable"] is False
    assert payload["send_enabled"] is False


class _Request:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def execute(self) -> object:
        return self.payload


class _SheetReadService:
    def __init__(
        self,
        *,
        metadata: dict[str, object],
        grid_by_range: dict[str, dict[str, object]] | None = None,
        values_by_mode: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.metadata = metadata
        self.grid_by_range = grid_by_range or {}
        self.values_by_mode = values_by_mode or {}
        self.calls: list[dict[str, object]] = []

    def spreadsheets(self) -> _SheetReadService:
        return self

    def values(self) -> _SheetReadService:
        return self

    def get(self, **kwargs: object) -> _Request:
        self.calls.append(dict(kwargs))
        render_option = str(kwargs.get("valueRenderOption") or "")
        if render_option:
            return _Request(self.values_by_mode.get(render_option, {"values": []}))
        if kwargs.get("includeGridData") is True:
            ranges = kwargs.get("ranges")
            read_range = str(ranges[0]) if isinstance(ranges, list) and ranges else ""
            return _Request(self.grid_by_range.get(read_range, {"sheets": []}))
        return _Request(self.metadata)


def _sheet_metadata(
    *,
    spreadsheet_id: str = "sheetSynthetic",
    sheet_id: int = 7,
    sheet_title: str = "Evidence",
    locale: str = "en_US",
    time_zone: str = "America/New_York",
    named_ranges: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "spreadsheetId": spreadsheet_id,
        "properties": {
            "title": "Evidence workbook",
            "locale": locale,
            "timeZone": time_zone,
        },
        "sheets": [
            {
                "properties": {
                    "sheetId": sheet_id,
                    "title": sheet_title,
                    "gridProperties": {"rowCount": 100, "columnCount": 20},
                }
            }
        ],
        "namedRanges": named_ranges or [],
    }


def _grid_cell(
    *,
    entered: dict[str, object] | None = None,
    effective: dict[str, object] | None = None,
    formatted: object | None = None,
    note: str = "",
    hyperlink: str = "",
) -> dict[str, object]:
    cell: dict[str, object] = {}
    if entered is not None:
        cell["userEnteredValue"] = entered
    if effective is not None:
        cell["effectiveValue"] = effective
    if formatted is not None:
        cell["formattedValue"] = formatted
    if note:
        cell["note"] = note
    if hyperlink:
        cell["hyperlink"] = hyperlink
    return cell


def _grid_payload(
    *,
    start_row: int,
    start_column: int,
    rows: list[list[dict[str, object]]],
    sheet_id: int = 7,
    sheet_title: str = "Evidence",
) -> dict[str, object]:
    return {
        "spreadsheetId": "sheetSynthetic",
        "sheets": [
            {
                "properties": {"sheetId": sheet_id, "title": sheet_title},
                "data": [
                    {
                        "startRow": start_row,
                        "startColumn": start_column,
                        "rowData": [{"values": row} for row in rows],
                    }
                ],
            }
        ],
    }


def _patch_sheet_read_services(
    monkeypatch: pytest.MonkeyPatch,
    service: _SheetReadService,
) -> None:
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": object(), "sheets": service},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_google_sheet_under_kniops",
        lambda *_: None,
    )


def test_sheet_read_bounds_named_range_and_advances_exact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata(
        named_ranges=[
            {
                "namedRangeId": "namedEvidence",
                "name": "EvidenceRange",
                "range": {
                    "sheetId": 7,
                    "startRowIndex": 4,
                    "endRowIndex": 7,
                    "startColumnIndex": 1,
                    "endColumnIndex": 4,
                },
            }
        ]
    )
    page_one = _grid_payload(
        start_row=4,
        start_column=1,
        rows=[
            [
                _grid_cell(
                    entered={"stringValue": "Name"},
                    effective={"stringValue": "Name"},
                    formatted="Name",
                ),
                _grid_cell(
                    entered={"stringValue": "Count"},
                    effective={"stringValue": "Count"},
                    formatted="Count",
                ),
                _grid_cell(
                    entered={"stringValue": "Condition"},
                    effective={"stringValue": "Condition"},
                    formatted="Condition",
                ),
            ],
            [
                _grid_cell(
                    entered={"stringValue": "Alpha"},
                    effective={"stringValue": "Alpha"},
                    formatted="Alpha",
                ),
                _grid_cell(entered={"numberValue": 0}, effective={"numberValue": 0}, formatted="0"),
                _grid_cell(
                    entered={"stringValue": "NOT approved"},
                    effective={"stringValue": "NOT approved"},
                    formatted="NOT approved",
                ),
            ],
        ],
    )
    page_two = _grid_payload(
        start_row=6,
        start_column=1,
        rows=[
            [
                _grid_cell(
                    entered={"stringValue": "Beta"},
                    effective={"stringValue": "Beta"},
                    formatted="Beta",
                ),
                _grid_cell(
                    entered={"numberValue": -4}, effective={"numberValue": -4}, formatted="-4"
                ),
                _grid_cell(
                    entered={"stringValue": "Only after review"},
                    effective={"stringValue": "Only after review"},
                    formatted="Only after review",
                ),
            ]
        ],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={
            "'Evidence'!B5:D6": page_one,
            "'Evidence'!B7:D7": page_two,
        },
    )
    _patch_sheet_read_services(monkeypatch, service)

    first = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="EvidenceRange",
        sheet_name="Evidence",
        max_rows=2,
        max_columns=3,
        live=True,
    )
    next_request = first["continuation"]["next_request"]  # type: ignore[index]
    assert isinstance(next_request, dict)
    second = google_sheet_read_table_impl(**next_request)

    assert first["requested_range"] == "EvidenceRange"
    assert first["resolved_range"] == "'Evidence'!B5:D7"
    assert first["range"] == "'Evidence'!B5:D6"
    assert first["named_range_id"] == "namedEvidence"
    assert first["rows"] == [
        ["Name", "Count", "Condition"],
        ["Alpha", "0", "NOT approved"],
    ]
    assert first["content_complete"] is False
    assert first["coverage"]["row_has_more"] is True  # type: ignore[index]
    assert first["cells"][3]["coordinate"] == "B6"  # type: ignore[index]
    assert first["cells"][4]["effective"] == {  # type: ignore[index]
        "type": "number",
        "value": 0,
    }
    assert second["range"] == "'Evidence'!B7:D7"
    assert second["rows"] == [["Beta", "-4", "Only after review"]]
    assert second["content_complete"] is True
    assert second["continuation"]["available"] is False  # type: ignore[index]
    assert [call["ranges"][0] for call in service.calls if call.get("includeGridData") is True] == [
        "'Evidence'!B5:D6",
        "'Evidence'!B7:D7",
    ]


def test_sheet_source_mode_distinguishes_formula_literal_and_source_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata(locale="en_GB", time_zone="Europe/London")
    grid = _grid_payload(
        start_row=0,
        start_column=0,
        rows=[
            [
                _grid_cell(
                    entered={"formulaValue": "=SUM(C1:C3)"},
                    effective={"numberValue": 1.2344},
                    formatted="£1.23",
                    note="Computed from source rows.",
                    hyperlink="https://example.com/source",
                ),
                _grid_cell(
                    entered={"stringValue": "=SUM(C1:C3)"},
                    effective={"stringValue": "=SUM(C1:C3)"},
                    formatted="=SUM(C1:C3)",
                ),
                _grid_cell(
                    entered={"numberValue": 46115},
                    effective={"numberValue": 46115},
                    formatted="03/04/2026",
                ),
                _grid_cell(
                    entered={"formulaValue": "=1/0"},
                    effective={
                        "errorValue": {"type": "DIVIDE_BY_ZERO", "message": "Division by zero"}
                    },
                    formatted="#DIV/0!",
                ),
            ]
        ],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:D1": grid},
    )
    _patch_sheet_read_services(monkeypatch, service)

    result = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:D1",
        max_rows=1,
        max_columns=4,
        representation="source",
        live=True,
    )

    by_coordinate = {cell["coordinate"]: cell for cell in result["cells"]}
    assert by_coordinate["A1"]["formula_provenance"] == "formula"
    assert by_coordinate["A1"]["entered"]["value"] == "=SUM(C1:C3)"
    assert by_coordinate["A1"]["effective"]["value"] == 1.2344
    assert by_coordinate["A1"]["display"] == "£1.23"
    assert by_coordinate["A1"]["note"] == "Computed from source rows."
    assert by_coordinate["A1"]["links"] == ["https://example.com/source"]
    assert by_coordinate["B1"]["formula_provenance"] == "literal"
    assert by_coordinate["B1"]["entered"]["type"] == "string"
    assert by_coordinate["C1"]["effective"]["value"] == 46115
    assert by_coordinate["C1"]["display"] == "03/04/2026"
    assert by_coordinate["D1"]["effective"]["type"] == "error"
    assert result["source_identity"]["locale"] == "en_GB"
    assert result["source_identity"]["time_zone"] == "Europe/London"
    assert result["semantic_complete"] is True


def test_sheet_cell_note_links_and_number_format_have_targeted_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_note = "Context detail. " * 65 + "NOT approved for external use."
    source_links = [f"https://example.com/source/{index}" for index in range(11)]
    raw_cell = {
        "userEnteredValue": {"numberValue": 46115},
        "effectiveValue": {"numberValue": 46115},
        "formattedValue": "03/04/2026 09:30",
        "effectiveFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "dd/mm/yyyy hh:mm"}},
        "note": long_note,
        "textFormatRuns": [
            {"startIndex": index, "format": {"link": {"uri": link}}}
            for index, link in enumerate(source_links)
        ],
    }
    metadata = _sheet_metadata(locale="en_GB", time_zone="Europe/London")
    grid = _grid_payload(
        start_row=0,
        start_column=0,
        rows=[[raw_cell]],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:A1": grid},
    )
    _patch_sheet_read_services(monkeypatch, service)

    first = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:A1",
        max_note_chars=500,
        max_links=10,
        live=True,
    )
    cell = first["cells"][0]
    note_request = cell["note_coverage"]["next_request"]
    link_request = cell["link_coverage"]["next_request"]
    note_chunks = [cell["note"]]
    note_starts = [cell["note_coverage"]["start"]]
    note_page = google_sheet_read_table_impl(**note_request)
    while True:
        note_cell = note_page["cells"][0]
        note_chunks.append(note_cell["note"])
        note_starts.append(note_cell["note_coverage"]["start"])
        next_note_request = note_cell["note_coverage"].get("next_request")
        if not next_note_request:
            break
        note_page = google_sheet_read_table_impl(**next_note_request)
    link_page = google_sheet_read_table_impl(**link_request)

    assert cell["number_format"] == {
        "type": "DATE_TIME",
        "pattern": "dd/mm/yyyy hh:mm",
    }
    assert cell["note_coverage"]["has_more"] is True
    assert cell["link_coverage"] == {
        "start": 0,
        "end": 10,
        "full_count": 11,
        "complete": False,
        "has_more": True,
        "next_request": link_request,
    }
    assert first["semantic_complete"] is False
    assert {item["kind"] for item in first["semantic_continuations"]} == {
        "cell_note",
        "cell_links",
    }
    assert "".join(note_chunks) == long_note
    assert note_starts == sorted(set(note_starts))
    assert "NOT approved for external use." in note_chunks[-1]
    assert note_page["cells"][0]["note_coverage"]["complete"] is True
    assert link_page["cells"][0]["links"] == [source_links[-1]]
    assert link_page["cells"][0]["link_coverage"]["complete"] is True
    grid_calls = [call for call in service.calls if call.get("includeGridData") is True]
    assert "effectiveFormat(numberFormat(type,pattern))" in str(grid_calls[0]["fields"])


@pytest.mark.parametrize(
    ("target_coordinate", "initial_request"),
    [
        ("B2", {"range_a1": "'Evidence'!B2:B2"}),
        ("A2", {"sheet_name": "Evidence", "max_rows": 1, "max_columns": 2}),
        ("B1", {"sheet_name": "Evidence", "max_rows": 2, "max_columns": 1}),
        ("B2", {"sheet_name": "Evidence", "max_rows": 1, "max_columns": 1}),
    ],
)
def test_sheet_later_page_semantic_detail_requests_rebase_to_exact_cell(
    monkeypatch: pytest.MonkeyPatch,
    target_coordinate: str,
    initial_request: dict[str, object],
) -> None:
    long_value = "Value detail. " * 45 + "NOT approved."
    long_note = "Note detail. " * 45 + "Independent review required."
    links = [f"https://example.test/reference/{index}" for index in range(11)]
    target = {
        "userEnteredValue": {"stringValue": long_value},
        "effectiveValue": {"stringValue": long_value},
        "formattedValue": long_value,
        "note": long_note,
        "textFormatRuns": [
            {"startIndex": index, "format": {"link": {"uri": link}}}
            for index, link in enumerate(links)
        ],
    }
    ordinary = _grid_cell(
        entered={"stringValue": "ordinary"},
        effective={"stringValue": "ordinary"},
        formatted="ordinary",
    )
    cells = {
        "A1": target if target_coordinate == "A1" else ordinary,
        "B1": target if target_coordinate == "B1" else ordinary,
        "A2": target if target_coordinate == "A2" else ordinary,
        "B2": target if target_coordinate == "B2" else ordinary,
    }
    metadata = _sheet_metadata()
    metadata["sheets"][0]["properties"]["gridProperties"] = {  # type: ignore[index]
        "rowCount": 2,
        "columnCount": 2,
    }
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={
            "'Evidence'!A1:B1": _grid_payload(
                start_row=0,
                start_column=0,
                rows=[[cells["A1"], cells["B1"]]],
            ),
            "'Evidence'!A2:B2": _grid_payload(
                start_row=1,
                start_column=0,
                rows=[[cells["A2"], cells["B2"]]],
            ),
            "'Evidence'!A1:A2": _grid_payload(
                start_row=0,
                start_column=0,
                rows=[[cells["A1"]], [cells["A2"]]],
            ),
            "'Evidence'!B1:B2": _grid_payload(
                start_row=0,
                start_column=1,
                rows=[[cells["B1"]], [cells["B2"]]],
            ),
            "'Evidence'!A1:A1": _grid_payload(
                start_row=0,
                start_column=0,
                rows=[[cells["A1"]]],
            ),
            "'Evidence'!B1:B1": _grid_payload(
                start_row=0,
                start_column=1,
                rows=[[cells["B1"]]],
            ),
            "'Evidence'!A2:A2": _grid_payload(
                start_row=1,
                start_column=0,
                rows=[[cells["A2"]]],
            ),
            "'Evidence'!B2:B2": _grid_payload(
                start_row=1,
                start_column=1,
                rows=[[cells["B2"]]],
            ),
        },
    )
    _patch_sheet_read_services(monkeypatch, service)

    page = google_sheet_read_table_impl(
        "sheetSynthetic",
        max_cells=1 if not initial_request.get("range_a1") else 100,
        live=True,
        **initial_request,
    )
    for _ in range(8):
        if any(cell["coordinate"] == target_coordinate for cell in page["cells"]):
            break
        next_request = page["continuation"]["next_request"]
        assert isinstance(next_request, dict)
        page = google_sheet_read_table_impl(**next_request)
    else:
        pytest.fail(f"{target_coordinate} was not reached")

    assert {item["kind"] for item in page["semantic_continuations"]} == {
        "cell_value",
        "cell_note",
        "cell_links",
    }
    for item in page["semantic_continuations"]:
        detail_request = item["next_request"]
        assert detail_request["range_a1"].endswith(
            f"!{target_coordinate}:{target_coordinate}"
        )
        assert detail_request["row_band_start"] == 0
        assert detail_request["column_band_start"] == 0
        detail = google_sheet_read_table_impl(**detail_request)
        assert [cell["coordinate"] for cell in detail["cells"]] == [target_coordinate]


def test_sheet_source_projection_is_compact_for_representative_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata()
    rows = [
        [
            _grid_cell(
                entered={"numberValue": row_index * 20 + column_index},
                effective={"numberValue": row_index * 20 + column_index},
                formatted=str(row_index * 20 + column_index),
            )
            for column_index in range(20)
        ]
        for row_index in range(100)
    ]
    first_page_grid = _grid_payload(
        start_row=0,
        start_column=0,
        rows=rows[:5],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:T5": first_page_grid},
    )
    _patch_sheet_read_services(monkeypatch, service)

    result = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:T100",
        max_rows=100,
        max_columns=20,
        live=True,
    )

    provider_chars = len(
        json.dumps(first_page_grid, ensure_ascii=False, separators=(",", ":"))
    )
    result_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    assert len(result["cells"]) == 100
    assert result["range"] == "'Evidence'!A1:T5"
    assert result["coverage"]["returned_rectangle_cells"] == 100
    assert result["coverage"]["response_density_bounded"] is False
    assert result["continuation"]["next_request"]["row_start"] == 5
    assert result["continuation"]["next_request"]["max_cells"] == 100
    assert result["representations"] == {
        "mode": "source",
        "canonical_source_cells": "cells",
        "formatted_rows": "rows",
    }
    assert result_chars <= provider_chars * 4


def test_sheet_long_cell_values_have_exact_same_cell_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_value = "Source qualification. " * 600 + "NOT approved for external use."
    grid = _grid_payload(
        start_row=0,
        start_column=0,
        rows=[
            [
                _grid_cell(
                    entered={"stringValue": long_value},
                    effective={"stringValue": long_value},
                    formatted=long_value,
                )
            ]
        ],
    )
    service = _SheetReadService(
        metadata=_sheet_metadata(),
        grid_by_range={"'Evidence'!A1:A1": grid},
    )
    _patch_sheet_read_services(monkeypatch, service)

    page = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:A1",
        max_value_chars=500,
        live=True,
    )
    chunks: dict[str, list[str]] = {"entered": [], "effective": [], "display": []}
    starts: list[int] = []
    while True:
        cell = page["cells"][0]
        chunks["entered"].append(cell["entered"]["value"])
        chunks["effective"].append(cell["effective"]["value"])
        chunks["display"].append(cell["display"])
        starts.append(cell["value_coverage"]["requested_start"])
        next_request = cell["value_coverage"].get("next_request")
        if not next_request:
            break
        assert next_request["range_a1"] == "'Evidence'!A1:A1"
        assert next_request["max_cells"] == 1
        page = google_sheet_read_table_impl(**next_request)

    assert "".join(chunks["entered"]) == long_value
    assert "".join(chunks["effective"]) == long_value
    assert "".join(chunks["display"]) == long_value
    assert starts == list(range(0, len(long_value), 500))
    assert page["semantic_complete"] is True
    assert page["semantic_continuations"] == []


def test_sheet_only_read_keeps_grid_coverage_beyond_first_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata()
    metadata["sheets"][0]["properties"]["gridProperties"] = {  # type: ignore[index]
        "rowCount": 4,
        "columnCount": 4,
    }
    grid = _grid_payload(
        start_row=0,
        start_column=0,
        rows=[
            [
                _grid_cell(
                    entered={"numberValue": row * 4 + column},
                    effective={"numberValue": row * 4 + column},
                    formatted=str(row * 4 + column),
                )
                for column in range(2)
            ]
            for row in range(2)
        ],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:B2": grid},
    )
    _patch_sheet_read_services(monkeypatch, service)

    result = google_sheet_read_table_impl(
        "sheetSynthetic",
        sheet_name="Evidence",
        max_rows=2,
        max_columns=2,
        live=True,
    )

    assert result["requested_range"] == ""
    assert result["resolved_range"] == "'Evidence'!A1:D4"
    assert result["range"] == "'Evidence'!A1:B2"
    assert result["content_complete"] is False
    assert result["coverage"]["row_has_more"] is True  # type: ignore[index]
    assert result["coverage"]["column_has_more"] is True  # type: ignore[index]
    assert result["continuation"]["next_request"]["range_a1"] == ""  # type: ignore[index]


def test_sheet_source_mode_preserves_blank_missing_and_column_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata(sheet_title="O'Brien Data")
    first_grid = _grid_payload(
        start_row=7,
        start_column=1,
        sheet_title="O'Brien Data",
        rows=[
            [
                _grid_cell(entered={"numberValue": 0}, effective={"numberValue": 0}, formatted="0"),
                _grid_cell(
                    entered={"boolValue": False}, effective={"boolValue": False}, formatted="FALSE"
                ),
            ]
        ],
    )
    second_grid = _grid_payload(
        start_row=7,
        start_column=3,
        sheet_title="O'Brien Data",
        rows=[
            [
                _grid_cell(
                    entered={"stringValue": ""}, effective={"stringValue": ""}, formatted=""
                ),
            ]
        ],
    )
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={
            "'O''Brien Data'!B8:C8": first_grid,
            "'O''Brien Data'!D8:E8": second_grid,
        },
    )
    _patch_sheet_read_services(monkeypatch, service)

    first = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'O''Brien Data'!B8:E8",
        max_rows=1,
        max_columns=2,
        live=True,
    )
    second = google_sheet_read_table_impl(
        **first["continuation"]["next_request"]  # type: ignore[index,arg-type]
    )

    assert first["range"] == "'O''Brien Data'!B8:C8"
    assert first["cells"][0]["effective"]["value"] == 0  # type: ignore[index]
    assert first["cells"][1]["effective"]["value"] is False  # type: ignore[index]
    assert second["range"] == "'O''Brien Data'!D8:E8"
    assert second["cells"][0]["entered"] == {  # type: ignore[index]
        "type": "string",
        "value": "",
    }
    assert second["missing_cells"] == ["E8"]
    assert second["content_complete"] is True


def test_sheet_identity_change_and_unavailable_named_range_do_not_become_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata()
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:A1": {"sheets": []}},
        values_by_mode={
            "FORMATTED_VALUE": {"range": "'Evidence'!A1:A1", "values": []},
            "UNFORMATTED_VALUE": {"range": "'Evidence'!A1:A1", "values": []},
            "FORMULA": {"range": "'Evidence'!A1:A1", "values": []},
        },
    )
    _patch_sheet_read_services(monkeypatch, service)

    empty = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:A1",
        live=True,
    )
    missing_named = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="UnknownRange",
        live=True,
    )
    changed_metadata = _sheet_metadata(locale="fr_FR")
    changed_service = _SheetReadService(metadata=changed_metadata)
    _patch_sheet_read_services(monkeypatch, changed_service)
    changed = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:A1",
        expected_source_identity_sha256=empty["source_identity"]["identity_sha256"],  # type: ignore[index]
        live=True,
    )

    assert empty["status"] == "success"
    assert empty["evidence_state"] == "empty"
    assert empty["content_complete"] is True
    assert empty["semantic_complete"] is False
    assert missing_named["status"] == "blocked"
    assert missing_named["reason"] == "named_range_not_found"
    assert missing_named["content_complete"] is False
    assert changed["status"] == "source_identity_changed"
    assert changed["content_complete"] is False


def test_sheet_title_resolution_blocks_same_title_distinct_workbook_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Files:
        def list(self, **_kwargs: object) -> _Request:
            return _Request(
                {
                    "files": [
                        {"id": "sheetOne", "name": "Shared title"},
                        {"id": "sheetTwo", "name": "Shared title"},
                    ]
                }
            )

    class Drive:
        def files(self) -> Files:
            return Files()

    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": Drive(), "sheets": object()},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_: "folderKNI",
    )

    with pytest.raises(RuntimeError, match="Multiple Google Sheets"):
        google_sheet_read_table_impl(
            title="Shared title",
            sheet_name="Evidence",
            live=True,
        )


def test_sheet_values_fallback_exposes_representations_without_guessing_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _sheet_metadata()
    service = _SheetReadService(
        metadata=metadata,
        grid_by_range={"'Evidence'!A1:B2": {"sheets": []}},
        values_by_mode={
            "FORMATTED_VALUE": {
                "range": "'Evidence'!A1:B2",
                "values": [["Measure", "Value"], ["Total", "$1.23"]],
            },
            "UNFORMATTED_VALUE": {
                "range": "'Evidence'!A1:B2",
                "values": [["Measure", "Value"], ["Total", 1.2344]],
            },
            "FORMULA": {
                "range": "'Evidence'!A1:B2",
                "values": [["Measure", "Value"], ["Total", "=SUM(C1:C3)"]],
            },
        },
    )
    _patch_sheet_read_services(monkeypatch, service)

    result = google_sheet_read_table_impl(
        "sheetSynthetic",
        range_a1="'Evidence'!A1:B2",
        representation="source",
        live=True,
    )

    assert result["rows"][1][1] == "$1.23"
    assert result["representations"]["effective_rows"] == "cells[].values.effective_rows"
    assert result["representations"]["formula_or_literal_rows"] == (
        "cells[].values.formula_or_literal_rows"
    )
    assert result["cells"][-1]["values"]["effective_rows"] == 1.2344  # type: ignore[index]
    assert result["cells"][-1]["values"]["formula_or_literal_rows"] == "=SUM(C1:C3)"  # type: ignore[index]
    assert result["cells"][-1]["formula_provenance"] == "unavailable_from_values_api"  # type: ignore[index]
    assert result["semantic_complete"] is False
    assert any("formula-versus-literal" in item for item in result["limitations"])


class _DocsDocuments:
    def __init__(self, payload: dict[str, object], captured: dict[str, object]) -> None:
        self.payload = payload
        self.captured = captured

    def get(self, **kwargs: object) -> _Request:
        self.captured.update(kwargs)
        return _Request(self.payload)


class _Docs:
    def __init__(self, payload: dict[str, object], captured: dict[str, object]) -> None:
        self.payload = payload
        self.captured = captured

    def documents(self) -> _DocsDocuments:
        return _DocsDocuments(self.payload, self.captured)


def _doc_paragraph(
    text: str = "",
    *,
    named_style: str = "",
    elements: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    paragraph: dict[str, object] = {
        "elements": elements
        if elements is not None
        else [{"textRun": {"content": text}}]
    }
    if named_style:
        paragraph["paragraphStyle"] = {"namedStyleType": named_style}
    return {"paragraph": paragraph}


def _doc_cell(
    *content: dict[str, object],
    row_span: int = 1,
    column_span: int = 1,
) -> dict[str, object]:
    cell: dict[str, object] = {"content": list(content)}
    if row_span != 1 or column_span != 1:
        cell["tableCellStyle"] = {
            "rowSpan": row_span,
            "columnSpan": column_span,
        }
    return cell


def _doc_table(rows: list[list[dict[str, object]]]) -> dict[str, object]:
    return {
        "table": {
            "rows": len(rows),
            "columns": max((len(row) for row in rows), default=0),
            "tableRows": [
                {"tableCells": row}
                for row in rows
            ],
        }
    }


def _read_synthetic_doc(
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object],
    *,
    max_chars: int = 6000,
    start_char: int = 0,
    semantic_start: int = 0,
    expected_revision_id: str = "",
    expected_snapshot_sha256: str = "",
    suggestions_view_mode: str | None = "SUGGESTIONS_INLINE",
) -> tuple[dict[str, object], dict[str, object]]:
    if suggestions_view_mode is not None:
        document.setdefault("suggestionsViewMode", suggestions_view_mode)
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"docs": _Docs(document, captured), "drive": object()},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_in_folder",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_start_google_workspace_read_attempt",
        lambda: None,
    )
    return (
        google_doc_read_impl(
            "docSyntheticFidelity",
            max_chars=max_chars,
            start_char=start_char,
            semantic_start=semantic_start,
            expected_revision_id=expected_revision_id,
            expected_snapshot_sha256=expected_snapshot_sha256,
            live=True,
        ),
        captured,
    )


def test_google_doc_read_recovers_table_facts_and_reports_legacy_tab_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Synthetic pilot plan",
        "body": {
            "content": [
                _doc_paragraph("Pilot overview\n"),
                _doc_table(
                    [
                        [
                            _doc_cell(_doc_paragraph("Status\n")),
                            _doc_cell(_doc_paragraph("Budget\n")),
                        ],
                        [
                            _doc_cell(_doc_paragraph("NOT approved\n")),
                            _doc_cell(_doc_paragraph("$0\n")),
                        ],
                    ]
                ),
            ]
        },
    }

    result, captured = _read_synthetic_doc(monkeypatch, document)
    text = str(result["text"])

    assert captured == {
        "documentId": "docSyntheticFidelity",
        "includeTabsContent": True,
        "suggestionsViewMode": "SUGGESTIONS_INLINE",
    }
    assert text.index("Pilot overview") < text.index("[[table 1 row 1]]")
    assert (
        "[[table 1 cell row=2 cell_index=1 row_span=1 column_span=1]]\n"
        "NOT approved"
    ) in text
    assert (
        "[[table 1 cell row=2 cell_index=2 row_span=1 column_span=1]]\n$0"
    ) in text
    assert result["truncated"] is False
    assert result["source_version"] == {"revision_id": "", "status": "unavailable"}
    assert result["supported_text_complete"] is True
    assert result["content_complete"] is False
    assert result["extraction_status"] == "partial"
    structure = result["structure"]
    assert structure["source_mode"] == "legacy_body"  # type: ignore[index]
    assert structure["counts"]["tables"] == 1  # type: ignore[index]
    assert any("did not include tab content" in item for item in result["limitations"])


def test_google_doc_read_preserves_nested_tables_tabs_and_labeled_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = _doc_table(
        [
            [_doc_cell(_doc_paragraph("Nested status\n"))],
            [_doc_cell(_doc_paragraph("NOT approved\n"))],
        ]
    )
    document = {
        "title": "Structured pilot plan",
        "revisionId": "revision-structured-7",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-main", "title": "Main"},
                "documentTab": {
                    "headers": {"header-1": {"content": [_doc_paragraph("Header: draft\n")]}},
                    "body": {
                        "content": [
                            _doc_paragraph("Pilot status\n", named_style="HEADING_1"),
                            _doc_table(
                                [
                                    [
                                        _doc_cell(_doc_paragraph("Metric\n")),
                                        _doc_cell(_doc_paragraph("Metric\n")),
                                        _doc_cell(_doc_paragraph("\n")),
                                    ],
                                    [
                                        _doc_cell(_doc_paragraph("Budget\n"), nested),
                                        _doc_cell(_doc_paragraph("0\n")),
                                        _doc_cell(_doc_paragraph("-4\n")),
                                    ],
                                    [
                                        _doc_cell(_doc_paragraph("Qualified finding\n")),
                                        _doc_cell(_doc_paragraph("Only if reviewed\n")),
                                    ],
                                ]
                            ),
                            {
                                "tableOfContents": {
                                    "content": [_doc_paragraph("Overview .... 1\n")]
                                }
                            },
                            _doc_paragraph("Final note: no deployment.\n"),
                        ]
                    },
                    "footnotes": {
                        "fn-1": {
                            "content": [_doc_paragraph("Footnote: zero means no budget.\n")]
                        }
                    },
                    "footers": {"footer-1": {"content": [_doc_paragraph("Footer source\n")]}},
                    "inlineObjects": {},
                    "positionedObjects": {},
                },
                "childTabs": [
                    {
                        "tabProperties": {"tabId": "tab-child", "title": "Appendix"},
                        "documentTab": {
                            "body": {
                                "content": [
                                    _doc_paragraph("Child tab: source limitation.\n")
                                ]
                            }
                        },
                        "childTabs": [],
                    }
                ],
            }
        ],
    }

    result, _ = _read_synthetic_doc(monkeypatch, document)
    text = str(result["text"])

    expected_order = [
        "[[tab 1; title=Main",
        "Header: draft",
        "Pilot status",
        "[[table 1 row 1]]",
        "[[table 1 cell row=1 cell_index=1 row_span=1 column_span=1]]\nMetric",
        "[[table 1 cell row=1 cell_index=2 row_span=1 column_span=1]]\nMetric",
        "[[table 1 cell row=1 cell_index=3 row_span=1 column_span=1]]",
        "[[table 2 row 2]]",
        "NOT approved",
        "[[table of contents 1 start]]",
        "Overview .... 1",
        "Final note: no deployment.",
        "Footnote: zero means no budget.",
        "Footer source",
        "[[tab 1.1; title=Appendix",
        "Child tab: source limitation.",
    ]
    positions = [text.index(value) for value in expected_order]
    assert positions == sorted(positions)
    assert "[[table 1 cell row=1 cell_index=3: blank]]" in text
    assert (
        "[[table 1 cell row=2 cell_index=2 row_span=1 column_span=1]]\n0"
    ) in text
    assert (
        "[[table 1 cell row=2 cell_index=3 row_span=1 column_span=1]]\n-4"
    ) in text
    assert "Only if reviewed" in text
    assert result["content_complete"] is True
    assert result["supported_text_complete"] is True
    assert result["visual_content_status"] == "not_present"
    assert result["limitations"] == []
    assert result["source_version"] == {
        "revision_id": "revision-structured-7",
        "status": "available",
    }
    structure = result["structure"]
    assert structure["source_mode"] == "tabs"  # type: ignore[index]
    assert structure["counts"]["tables"] == 2  # type: ignore[index]
    assert structure["counts"]["footnotes"] == 1  # type: ignore[index]
    assert [tab["position"] for tab in structure["tabs"]] == ["1", "1.1"]  # type: ignore[index]


def test_google_doc_read_preserves_merged_cell_spans_without_inferred_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Merged table",
        "revisionId": "revision-merged-2",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-merged", "title": "Merged"},
                "documentTab": {
                    "body": {
                        "content": [
                            _doc_table(
                                [
                                    [
                                        _doc_cell(
                                            _doc_paragraph("Combined status\n"),
                                            row_span=2,
                                            column_span=2,
                                        ),
                                        _doc_cell(_doc_paragraph("Owner\n")),
                                    ],
                                    [_doc_cell(_doc_paragraph("Review team\n"))],
                                ]
                            )
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }

    result, _ = _read_synthetic_doc(monkeypatch, document)
    text = str(result["text"])

    assert (
        "[[table 1 cell row=1 cell_index=1 row_span=2 column_span=2]]\n"
        "Combined status"
    ) in text
    assert " column=" not in text
    assert result["content_complete"] is False
    assert result["supported_text_complete"] is True
    assert result["source_version"]["revision_id"] == "revision-merged-2"  # type: ignore[index]
    structure = result["structure"]
    assert structure["merged_cell_count"] == 1  # type: ignore[index]
    assert structure["table_geometry_status"] == "partial_merged_cells"  # type: ignore[index]
    assert any("logical grid columns" in item for item in result["limitations"])


def test_google_doc_read_preserves_links_strikethrough_and_suggestion_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Semantic revision example",
        "revisionId": "revision-semantic-4",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-semantic", "title": "Review"},
                "documentTab": {
                    "body": {
                        "content": [
                            _doc_paragraph(
                                elements=[
                                    {
                                        "startIndex": 1,
                                        "endIndex": 9,
                                        "textRun": {
                                            "content": "Approved",
                                            "textStyle": {"strikethrough": True},
                                        },
                                    },
                                    {
                                        "startIndex": 9,
                                        "endIndex": 32,
                                        "textRun": {
                                            "content": " pending review. Source",
                                            "textStyle": {
                                                "link": {
                                                    "url": "https://example.org/source-proof"
                                                }
                                            },
                                        },
                                    },
                                    {
                                        "startIndex": 32,
                                        "endIndex": 67,
                                        "textRun": {
                                            "content": " [[strikethrough=true]] literal",
                                        },
                                    },
                                    {
                                        "startIndex": 67,
                                        "endIndex": 85,
                                        "textRun": {
                                            "content": " Proposed addition",
                                            "suggestedInsertionIds": ["suggest-insert"],
                                        },
                                    },
                                    {
                                        "startIndex": 85,
                                        "endIndex": 100,
                                        "textRun": {
                                            "content": " Old amount $10",
                                            "suggestedDeletionIds": ["suggest-delete"],
                                        },
                                    },
                                    {
                                        "startIndex": 100,
                                        "endIndex": 107,
                                        "textRun": {
                                            "content": " Status",
                                            "suggestedTextStyleChanges": {
                                                "suggest-style": {
                                                    "textStyle": {
                                                        "strikethrough": True,
                                                        "link": {
                                                            "url": (
                                                                "https://example.org/"
                                                                "suggested-proof"
                                                            )
                                                        },
                                                    },
                                                    "textStyleSuggestionState": {
                                                        "strikethroughSuggested": True,
                                                        "linkSuggested": True,
                                                    },
                                                }
                                            },
                                        },
                                    },
                                ]
                            )
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }

    result, captured = _read_synthetic_doc(monkeypatch, document)
    text = str(result["text"])
    annotations = result["semantic_annotations"]

    assert captured["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    assert "Approved pending review. Source" in text
    assert "[[strikethrough=true]] literal" in text
    assert "https://example.org/source-proof" not in text
    assert result["semantic_coverage"] == {
        "suggestions_view_mode": "SUGGESTIONS_INLINE",
        "supported": True,
        "complete": True,
        "annotations_truncated": False,
        "annotation_count": 5,
        "document_annotation_count": 5,
        "returned_annotation_count": 5,
    }
    assert result["content_complete"] is True
    assert annotations[0]["strikethrough"] is True
    assert annotations[0]["change_state"] == "base_content"
    assert annotations[0]["provider_range"]["start_index"] == 1
    assert annotations[0]["source_location"]["tab_id"] == "tab-semantic"
    assert annotations[1]["link"] == {
        "type": "url",
        "url": "https://example.org/source-proof",
    }
    assert annotations[2]["change_state"] == "suggested_insertion"
    assert annotations[2]["suggested_insertion_ids"] == ["suggest-insert"]
    assert annotations[3]["change_state"] == "suggested_deletion"
    assert annotations[3]["suggested_deletion_ids"] == ["suggest-delete"]
    assert annotations[4]["suggested_style_changes"] == [
        {
            "suggestion_id": "suggest-style",
            "strikethrough": True,
            "link": {
                "type": "url",
                "url": "https://example.org/suggested-proof",
            },
        }
    ]


def test_google_doc_read_marks_unconfirmed_or_malformed_semantics_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_mode, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Unconfirmed suggestions",
            "revisionId": "revision-unconfirmed",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-one", "title": "One"},
                    "documentTab": {
                        "body": {"content": [_doc_paragraph("Current")]}
                    },
                    "childTabs": [],
                }
            ],
        },
        suggestions_view_mode=None,
    )
    malformed_link, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Malformed link",
            "revisionId": "revision-malformed-link",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-two", "title": "Two"},
                    "documentTab": {
                        "body": {
                            "content": [
                                _doc_paragraph(
                                    elements=[
                                        {
                                            "textRun": {
                                                "content": "Source",
                                                "textStyle": {"link": {}},
                                            }
                                        }
                                    ]
                                )
                            ]
                        }
                    },
                    "childTabs": [],
                }
            ],
        },
    )

    assert missing_mode["content_complete"] is False
    assert missing_mode["supported_text_complete"] is False
    assert missing_mode["semantic_coverage"]["complete"] is False
    assert any("SUGGESTIONS_INLINE" in item for item in missing_mode["limitations"])
    assert malformed_link["content_complete"] is False
    assert malformed_link["semantic_annotations"] == []
    assert any("destination" in item for item in malformed_link["limitations"])


def test_google_doc_read_oversized_semantic_payload_is_bounded_and_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Oversized link",
            "revisionId": "revision-oversized-link",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-link", "title": "Link"},
                    "documentTab": {
                        "body": {
                            "content": [
                                _doc_paragraph(
                                    elements=[
                                        {
                                            "textRun": {
                                                "content": "Source",
                                                "textStyle": {
                                                    "link": {
                                                        "url": "https://example.org/"
                                                        + ("x" * 5000)
                                                    }
                                                },
                                            }
                                        }
                                    ]
                                )
                            ]
                        }
                    },
                    "childTabs": [],
                }
            ],
        },
    )

    annotation = result["semantic_annotations"][0]  # type: ignore[index]
    assert result["semantic_coverage"]["complete"] is False  # type: ignore[index]
    assert result["content_complete"] is False
    assert result["continuation"]["available"] is False  # type: ignore[index]
    assert annotation["semantic_payload_omitted"] is True
    assert annotation["link_present_but_omitted"] is True
    assert len(json.dumps(annotation, sort_keys=True)) < 4000
    assert any("payload was omitted" in item for item in result["limitations"])


def test_google_doc_read_marks_image_only_content_as_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Image-only brief",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-image", "title": "Image"},
                "documentTab": {
                    "body": {
                        "content": [
                            _doc_paragraph(
                                elements=[
                                    {"textRun": {"content": "Claim before visual. "}},
                                    {"inlineObjectElement": {"inlineObjectId": "image-1"}},
                                    {
                                        "textRun": {
                                            "content": " Qualifier after visual: NOT approved."
                                        }
                                    },
                                ]
                            ),
                            _doc_table(
                                [
                                    [
                                        _doc_cell(
                                            _doc_paragraph(elements=[{"equation": {}}])
                                        )
                                    ]
                                ]
                            ),
                        ]
                    },
                    "inlineObjects": {"image-1": {"inlineObjectProperties": {}}},
                },
                "childTabs": [],
            }
        ],
    }

    result, _ = _read_synthetic_doc(monkeypatch, document)

    text = str(result["text"])
    assert text.index("Claim before visual.") < text.index(
        "[[inline object: visual content not read]]"
    ) < text.index("Qualifier after visual: NOT approved.")
    assert (
        "[[table 1 cell row=1 cell_index=1 row_span=1 column_span=1]]\n"
        "[[equation: content not interpreted]]"
    ) in text
    assert "[[table 1 cell row=1 cell_index=1: blank]]" not in text
    assert result["truncated"] is False
    assert result["content_complete"] is False
    assert result["supported_text_complete"] is False
    assert result["visual_content_status"] == "not_read"
    assert result["structure"]["unsupported_elements"] == {  # type: ignore[index]
        "equation": 1,
        "inline_object": 1,
    }
    assert any("visual content was not read" in item for item in result["limitations"])


def test_google_doc_read_handles_empty_malformed_and_character_limited_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Empty",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-empty", "title": "Empty"},
                    "documentTab": {"body": {"content": []}},
                    "childTabs": [],
                }
            ],
        },
    )
    assert empty["content_complete"] is True
    assert empty["extracted_char_count"] > 0  # structural annotation, not source text

    malformed, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Malformed",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-bad", "title": "Malformed"},
                    "documentTab": {
                        "body": {"content": [None, {"unsupportedWidget": {}}]}
                    },
                    "childTabs": [],
                }
            ],
        },
    )
    assert malformed["content_complete"] is False
    assert malformed["structure"]["malformed_node_count"] == 1  # type: ignore[index]
    assert malformed["structure"]["unsupported_elements"] == {  # type: ignore[index]
        "structural_element:unsupportedWidget": 1
    }

    limited, _ = _read_synthetic_doc(
        monkeypatch,
        {
            "title": "Long",
            "tabs": [
                {
                    "tabProperties": {"tabId": "tab-long", "title": "Long"},
                    "documentTab": {
                        "body": {"content": [_doc_paragraph("A" * 1800)]}
                    },
                    "childTabs": [],
                }
            ],
        },
        max_chars=1000,
    )
    assert limited["truncated"] is True
    assert limited["content_complete"] is False
    assert limited["supported_text_complete"] is False
    assert len(str(limited["text"])) <= 1000
    assert limited["read_window"]["has_more"] is True
    assert limited["continuation"]["available"] is True
    assert limited["continuation"]["next_request"]["start_char"] == 1000
    assert limited["continuation"]["next_request"]["expected_revision_id"] == ""
    assert (
        limited["continuation"]["next_request"]["expected_snapshot_sha256"]
        == limited["source_snapshot"]["sha256"]
    )
    assert any("response window" in item for item in limited["limitations"])
    assert any("snapshot digest" in item for item in limited["limitations"])


def test_google_doc_read_continuation_has_no_gaps_or_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Long source",
        "revisionId": "revision-long-9",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-long", "title": "Long"},
                "documentTab": {
                    "body": {
                        "content": [
                            _doc_paragraph("A" * 1400),
                            _doc_paragraph(
                                "Late qualifier: NOT approved; budget is $0; unit is 5 µg."
                            ),
                            _doc_paragraph("Z" * 1200),
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }
    expected_document = dict(document)
    expected_document["suggestionsViewMode"] = "SUGGESTIONS_INLINE"
    expected_text = str(internal_data_tools._google_doc_extraction(expected_document)["text"])

    pages: list[dict[str, object]] = []
    start_char = 0
    semantic_start = 0
    expected_revision_id = ""
    expected_snapshot_sha256 = ""
    while True:
        page, _ = _read_synthetic_doc(
            monkeypatch,
            document,
            max_chars=1000,
            start_char=start_char,
            semantic_start=semantic_start,
            expected_revision_id=expected_revision_id,
            expected_snapshot_sha256=expected_snapshot_sha256,
        )
        pages.append(page)
        if not page["continuation"]["available"]:  # type: ignore[index]
            break
        next_request = page["continuation"]["next_request"]  # type: ignore[index]
        start_char = next_request["start_char"]  # type: ignore[index]
        semantic_start = next_request["semantic_start"]  # type: ignore[index]
        expected_revision_id = next_request["expected_revision_id"]  # type: ignore[index]
        expected_snapshot_sha256 = next_request["expected_snapshot_sha256"]  # type: ignore[index]

    combined = "".join(str(page["text"]) for page in pages)
    windows = [page["read_window"] for page in pages]
    repeated, _ = _read_synthetic_doc(
        monkeypatch,
        document,
        max_chars=1000,
        start_char=pages[1]["read_window"]["start_char"],  # type: ignore[index]
        semantic_start=pages[1]["semantic_window"]["start"],  # type: ignore[index]
        expected_revision_id="revision-long-9",
        expected_snapshot_sha256=pages[0]["source_snapshot"]["sha256"],  # type: ignore[index]
    )

    assert combined == expected_text
    assert "Late qualifier: NOT approved; budget is $0; unit is 5 µg." in combined
    assert [window["start_char"] for window in windows] == [  # type: ignore[index]
        0,
        1000,
        2000,
    ]
    assert all(
        windows[index]["end_char"] == windows[index + 1]["start_char"]  # type: ignore[index]
        for index in range(len(windows) - 1)
    )
    assert len({window["part_id"] for window in windows}) == len(windows)  # type: ignore[index]
    assert repeated["text"] == pages[1]["text"]
    assert repeated["read_window"] == pages[1]["read_window"]


def test_google_doc_read_semantic_continuation_reaches_late_link_and_strike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elements: list[dict[str, object]] = []
    provider_index = 1
    for index in range(16):
        content = f"Section {index:02d}: " + ("Reference background. " * 6)
        elements.append(
            {
                "startIndex": provider_index,
                "endIndex": provider_index + len(content),
                "textRun": {
                    "content": content,
                    "textStyle": {
                        "link": {"url": f"https://example.org/reference/{index}"},
                        "strikethrough": index == 15,
                    },
                },
            }
        )
        provider_index += len(content)
    document = {
        "title": "Many linked sections",
        "revisionId": "stable-semantic-revision",
        "tabs": [
            {
                "tabProperties": {"tabId": "source-tab", "title": "Source"},
                "documentTab": {"body": {"content": [_doc_paragraph(elements=elements)]}},
                "childTabs": [],
            }
        ],
    }

    pages: list[dict[str, object]] = []
    request: dict[str, object] = {"max_chars": 1000}
    while True:
        page, _ = _read_synthetic_doc(monkeypatch, document, **request)  # type: ignore[arg-type]
        pages.append(page)
        if not page["continuation"]["available"]:  # type: ignore[index]
            break
        request = dict(page["continuation"]["next_request"])  # type: ignore[arg-type,index]

    annotations = [
        annotation
        for page in pages
        for annotation in page["semantic_annotations"]  # type: ignore[union-attr]
    ]
    links = [annotation["link"]["url"] for annotation in annotations]  # type: ignore[index]

    assert "Section 15" in "".join(str(page["text"]) for page in pages)
    assert links == [f"https://example.org/reference/{index}" for index in range(16)]
    assert len({annotation["annotation_id"] for annotation in annotations}) == 16
    assert annotations[-1]["strikethrough"] is True
    assert "Section 15" in annotations[-1]["source_text"]["excerpt"]  # type: ignore[index]
    assert pages[0]["semantic_window"]["has_more"] is True  # type: ignore[index]
    assert pages[-1]["semantic_window"]["has_more"] is False  # type: ignore[index]


def test_google_doc_read_semantic_only_continuation_survives_text_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elements: list[dict[str, object]] = []
    provider_index = 1
    for index in range(18):
        content = f"L{index:02d} "
        elements.append(
            {
                "startIndex": provider_index,
                "endIndex": provider_index + len(content),
                "textRun": {
                    "content": content,
                    "textStyle": {
                        "link": {"url": f"https://example.org/dense/{index}"}
                    },
                },
            }
        )
        provider_index += len(content)
    document = {
        "title": "Dense semantic source",
        "revisionId": "dense-semantic-revision",
        "tabs": [
            {
                "tabProperties": {"tabId": "dense-tab", "title": "Dense"},
                "documentTab": {"body": {"content": [_doc_paragraph(elements=elements)]}},
                "childTabs": [],
            }
        ],
    }

    first, _ = _read_synthetic_doc(monkeypatch, document, max_chars=1000)
    assert first["read_window"]["has_more"] is False  # type: ignore[index]
    assert first["semantic_window"]["has_more"] is True  # type: ignore[index]
    assert first["continuation"]["available"] is True  # type: ignore[index]

    pages = [first]
    while pages[-1]["continuation"]["available"]:  # type: ignore[index]
        request = pages[-1]["continuation"]["next_request"]  # type: ignore[index]
        page, _ = _read_synthetic_doc(
            monkeypatch,
            document,
            max_chars=request["max_chars"],  # type: ignore[index]
            start_char=request["start_char"],  # type: ignore[index]
            semantic_start=request["semantic_start"],  # type: ignore[index]
            expected_revision_id=request["expected_revision_id"],  # type: ignore[index]
            expected_snapshot_sha256=request["expected_snapshot_sha256"],  # type: ignore[index]
        )
        pages.append(page)

    annotations = [
        annotation
        for page in pages
        for annotation in page["semantic_annotations"]  # type: ignore[union-attr]
    ]
    assert len(pages) > 1
    assert all(page["text"] == "" for page in pages[1:])
    assert [annotation["source_order"] for annotation in annotations] == list(
        range(1, 19)
    )
    assert len({annotation["annotation_id"] for annotation in annotations}) == 18
    assert pages[-1]["continuation"]["available"] is False  # type: ignore[index]


def test_google_doc_read_continuation_refuses_revision_mix_or_unpinned_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "title": "Changing source",
        "revisionId": "revision-before",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-change", "title": "Change"},
                "documentTab": {
                    "body": {"content": [_doc_paragraph("A" * 1800)]}
                },
                "childTabs": [],
            }
        ],
    }
    first, _ = _read_synthetic_doc(monkeypatch, original, max_chars=1000)
    next_start = first["continuation"]["next_request"]["start_char"]  # type: ignore[index]

    unpinned, _ = _read_synthetic_doc(
        monkeypatch,
        original,
        max_chars=1000,
        start_char=next_start,
    )
    changed = dict(original)
    changed["revisionId"] = "revision-after"
    changed["tabs"] = original["tabs"]
    source_changed, _ = _read_synthetic_doc(
        monkeypatch,
        changed,
        max_chars=1000,
        start_char=next_start,
        expected_revision_id="revision-before",
    )

    assert unpinned["status"] == "continuation_identity_required"
    assert unpinned["text"] == ""
    assert source_changed["status"] == "source_changed"
    assert source_changed["reason_code"] == "source_revision_changed"
    assert source_changed["text"] == ""
    assert source_changed["source_version"]["revision_id"] == "revision-after"  # type: ignore[index]


def test_google_doc_read_semantic_continuation_refuses_disappeared_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Revision unavailable",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-source", "title": "Source"},
                "documentTab": {
                    "body": {
                        "content": [
                            _doc_paragraph(
                                elements=[
                                    {
                                        "textRun": {
                                            "content": "Source",
                                            "textStyle": {
                                                "link": {
                                                    "url": "https://example.org/source"
                                                }
                                            },
                                        }
                                    }
                                ]
                            )
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }

    result, _ = _read_synthetic_doc(
        monkeypatch,
        document,
        semantic_start=1,
        expected_revision_id="revision-before",
    )

    assert result["status"] == "continuation_unavailable"
    assert result["reason_code"] == "source_revision_unavailable"
    assert result["text"] == ""


def test_google_doc_read_uses_snapshot_digest_when_revision_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "title": "Read-only source",
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-readonly", "title": "Read only"},
                "documentTab": {
                    "body": {"content": [_doc_paragraph("A" * 1600 + " LATE $0")]}
                },
                "childTabs": [],
            }
        ],
    }
    first, _ = _read_synthetic_doc(monkeypatch, document, max_chars=1000)
    next_request = first["continuation"]["next_request"]  # type: ignore[index]
    second, _ = _read_synthetic_doc(
        monkeypatch,
        document,
        max_chars=next_request["max_chars"],  # type: ignore[index]
        start_char=next_request["start_char"],  # type: ignore[index]
        expected_snapshot_sha256=next_request["expected_snapshot_sha256"],  # type: ignore[index]
    )
    changed = dict(document)
    changed["tabs"] = [
        {
            "tabProperties": {"tabId": "tab-readonly", "title": "Read only"},
            "documentTab": {
                "body": {"content": [_doc_paragraph("B" * 1600 + " CHANGED")]}
            },
            "childTabs": [],
        }
    ]
    changed_result, _ = _read_synthetic_doc(
        monkeypatch,
        changed,
        max_chars=1000,
        start_char=next_request["start_char"],  # type: ignore[index]
        expected_snapshot_sha256=next_request["expected_snapshot_sha256"],  # type: ignore[index]
    )

    assert second["status"] == "success"
    assert "LATE $0" in second["text"]
    assert changed_result["status"] == "source_changed"
    assert changed_result["reason_code"] == "source_snapshot_changed"
    assert changed_result["text"] == ""


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
