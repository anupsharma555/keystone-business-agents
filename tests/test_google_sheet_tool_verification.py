from __future__ import annotations

from typing import Any

from keystone_agents.tools.internal_data_tools import (
    GOOGLE_SHEET_MIME_TYPE,
    _verify_google_sheet_deleted_row,
    _verify_google_sheet_file,
    _verify_google_sheet_keyed_row,
    _verify_google_sheet_range,
)


class _Executable:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _Values:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads

    def get(self, *, range: str, **_kwargs: Any) -> _Executable:
        return _Executable(self.payloads.get(range, {"values": []}))


class _Spreadsheets:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self._values = _Values(payloads)

    def values(self) -> _Values:
        return self._values


class _SheetsService:
    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self._spreadsheets = _Spreadsheets(payloads)

    def spreadsheets(self) -> _Spreadsheets:
        return self._spreadsheets


class _Files:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def get(self, **_kwargs: Any) -> _Executable:
        return _Executable(self.payload)


class _DriveService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._files = _Files(payload)

    def files(self) -> _Files:
        return self._files


def test_google_sheet_file_verification_checks_identity_title_type_and_trash() -> None:
    service = _DriveService(
        {
            "id": "sheet-123",
            "name": "KBA_TEST_SHEET validation",
            "mimeType": GOOGLE_SHEET_MIME_TYPE,
            "trashed": False,
        }
    )

    result = _verify_google_sheet_file(
        service,
        "sheet-123",
        expected_title="KBA_TEST_SHEET validation",
        expected_trashed=False,
    )

    assert result["passed"] is True
    assert result["spreadsheet_id_match"] is True
    assert result["title_match"] is True
    assert result["trashed"] is False


def test_google_sheet_range_verification_reads_exact_appended_range() -> None:
    service = _SheetsService(
        {"Validation!A2:C2": {"values": [["KBA_TEST_ROW_1", "created", "TRUE"]]}}
    )

    result = _verify_google_sheet_range(
        service,
        "sheet-123",
        "Validation!A2:C2",
        expected_values=[["KBA_TEST_ROW_1", "created", True]],
    )

    assert result == {
        "status": "verified",
        "passed": True,
        "row_count_match": True,
        "values_match": True,
        "verified_row_count": 1,
    }


def test_google_sheet_range_verification_allows_provider_omitted_trailing_blanks() -> None:
    service = _SheetsService({"Validation!A2:C2": {"values": [["KBA_TEST_ROW_1"]]}})

    result = _verify_google_sheet_range(
        service,
        "sheet-123",
        "Validation!A2:C2",
        expected_values=[["KBA_TEST_ROW_1", "", None]],
    )

    assert result["passed"] is True
    assert result["values_match"] is True


def test_google_sheet_keyed_row_verification_reports_field_mismatch() -> None:
    service = _SheetsService(
        {
            "'Validation'!A:ZZ": {
                "values": [
                    ["record_key", "status", "note"],
                    ["KBA_TEST_ROW_1", "modified", "old note"],
                ]
            }
        }
    )

    result = _verify_google_sheet_keyed_row(
        service,
        "sheet-123",
        "Validation",
        key_column="record_key",
        key_value="KBA_TEST_ROW_1",
        expected_fields={"status": "modified", "note": "new note"},
    )

    assert result["passed"] is False
    assert result["row_found"] is True
    assert result["matched_fields"] == ["status"]
    assert result["mismatched_fields"] == ["note"]


def test_google_sheet_delete_verification_requires_count_delta_and_key_absence() -> None:
    before = [
        ["record_key", "status"],
        ["KBA_TEST_ROW_1", "modified"],
        ["other", "kept"],
    ]
    after = [["record_key", "status"], ["other", "kept"]]

    result = _verify_google_sheet_deleted_row(
        before,
        after,
        deleted_row_index=2,
        key_column="record_key",
        key_value="KBA_TEST_ROW_1",
    )

    assert result["passed"] is True
    assert result["row_count_delta"] == 1
    assert result["row_absent"] is True
