"""Run one bounded Google Sheet create/read/update/delete/trash lifecycle."""

from __future__ import annotations

import argparse
import json
import os
from uuid import uuid4

from keystone_agents.config import (
    parse_bool,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.tools.internal_data_tools import (
    google_drive_get_file_metadata_impl,
    google_sheet_append_rows_impl,
    google_sheet_create_impl,
    google_sheet_delete_rows_impl,
    google_sheet_read_table_impl,
    google_sheet_trash_impl,
    google_sheet_update_row_impl,
)

GOOGLE_SHEET_TEST_MARKER = "KBA_TEST_SHEET"
GOOGLE_SHEET_TEST_ROW_MARKER = "KBA_TEST_ROW"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder-path", default="KNIOps")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-google-workspace", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_google_workspace:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-google-workspace",
            live_action=(
                "creating, modifying, verifying, and trashing one marked Google Sheet"
            ),
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-google-workspace.")
    live = bool(args.live_google_workspace and not args.dry_run)
    if live and not parse_bool(os.getenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE")):
        raise SystemExit(
            "Live Google Sheet lifecycle is disabled. Set "
            "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true for the approved process."
        )

    suffix = uuid4().hex[:10]
    approval_reference = args.approval_reference.strip() or f"anu-199-{suffix}"
    result = execute_google_sheet_test_lifecycle(
        suffix=suffix,
        folder_path=args.folder_path,
        approval_reference=approval_reference,
        live=live,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "dry-run"} else 1


def execute_google_sheet_test_lifecycle(
    *,
    suffix: str,
    folder_path: str,
    approval_reference: str,
    live: bool,
) -> dict[str, object]:
    title = f"{GOOGLE_SHEET_TEST_MARKER} {suffix}"
    row_key = f"{GOOGLE_SHEET_TEST_ROW_MARKER}_{suffix}"
    spreadsheet_id = ""
    receipts: dict[str, object] = {}
    failure = ""
    try:
        created = google_sheet_create_impl(
            title,
            folder_path=folder_path,
            tabs_json='["Validation"]',
            approval_reference=f"{approval_reference}:create",
            live=live,
        )
        receipts["create"] = _write_receipt(created)
        spreadsheet_id = str(created.get("spreadsheet_id") or "")
        if not live:
            return {
                "status": "dry-run",
                "openai_requests": 0,
                "title": title,
                "receipts": receipts,
            }
        if not spreadsheet_id:
            raise RuntimeError("Google Sheet create returned no spreadsheet ID.")

        metadata = google_drive_get_file_metadata_impl(
            spreadsheet_id,
            folder_path=folder_path,
            live=True,
        )
        metadata_file = metadata.get("file", {})
        create_verified = bool(
            metadata.get("status") == "success"
            and isinstance(metadata_file, dict)
            and metadata_file.get("name") == title
            and metadata.get("trashed") is False
        )
        receipts["create_readback"] = {
            "status": "verified" if create_verified else "verification_failed",
            "passed": create_verified,
        }
        if not create_verified:
            raise RuntimeError("Google Sheet create metadata read-back failed.")

        appended = google_sheet_append_rows_impl(
            json.dumps(
                [
                    {
                        "record_key": row_key,
                        "status": "created",
                        "note": f"{GOOGLE_SHEET_TEST_ROW_MARKER} disposable row",
                    }
                ]
            ),
            spreadsheet_id_or_url=spreadsheet_id,
            title=title,
            folder_path=folder_path,
            sheet_name="Validation",
            approval_reference=f"{approval_reference}:append",
            live=True,
        )
        receipts["append"] = _write_receipt(appended)
        append_read = _read_test_row(
            spreadsheet_id=spreadsheet_id,
            title=title,
            folder_path=folder_path,
            row_key=row_key,
        )
        append_verified = append_read.get("status") == "created"
        receipts["append_readback"] = {
            "status": "verified" if append_verified else "verification_failed",
            "passed": append_verified,
            "row_found": bool(append_read),
        }
        if not append_verified:
            raise RuntimeError("Google Sheet appended row did not pass read-back verification.")

        updated = google_sheet_update_row_impl(
            json.dumps({"status": "modified", "note": "KBA_TEST_ROW updated"}),
            spreadsheet_id_or_url=spreadsheet_id,
            title=title,
            folder_path=folder_path,
            sheet_name="Validation",
            key_column="record_key",
            key_value=row_key,
            approval_reference=f"{approval_reference}:update",
            live=True,
        )
        receipts["update"] = _write_receipt(updated)
        update_read = _read_test_row(
            spreadsheet_id=spreadsheet_id,
            title=title,
            folder_path=folder_path,
            row_key=row_key,
        )
        update_verified = update_read.get("status") == "modified"
        receipts["update_readback"] = {
            "status": "verified" if update_verified else "verification_failed",
            "passed": update_verified,
            "row_found": bool(update_read),
        }
        if not update_verified:
            raise RuntimeError("Google Sheet updated row did not pass read-back verification.")

        deleted = google_sheet_delete_rows_impl(
            spreadsheet_id,
            title=title,
            folder_path=folder_path,
            sheet_name="Validation",
            key_column="record_key",
            key_value=row_key,
            approval_reference=f"{approval_reference}:delete-row",
            live=True,
        )
        receipts["delete_row"] = _write_receipt(deleted)
        delete_read = _read_test_row(
            spreadsheet_id=spreadsheet_id,
            title=title,
            folder_path=folder_path,
            row_key=row_key,
        )
        delete_verified = not delete_read
        receipts["delete_row_readback"] = {
            "status": "verified" if delete_verified else "verification_failed",
            "passed": delete_verified,
            "row_absent": delete_verified,
        }
        if not delete_verified:
            raise RuntimeError("Google Sheet row remained after cleanup.")
    except Exception as exc:  # cleanup still runs below
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and spreadsheet_id:
            try:
                trashed = google_sheet_trash_impl(
                    spreadsheet_id,
                    approval_reference=f"{approval_reference}:trash",
                    live=True,
                )
                receipts["trash"] = _write_receipt(trashed)
                trash_metadata = google_drive_get_file_metadata_impl(
                    spreadsheet_id,
                    folder_path=folder_path,
                    live=True,
                )
                trash_verified = bool(
                    trash_metadata.get("status") == "success"
                    and trash_metadata.get("trashed") is True
                )
                receipts["trash_readback"] = {
                    "status": "verified" if trash_verified else "verification_failed",
                    "passed": trash_verified,
                    "trashed": bool(trash_metadata.get("trashed")),
                }
                if not trash_verified and not failure:
                    failure = "RuntimeError: Google Sheet trash read-back failed."
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {exc}"
                failure = f"{failure}; cleanup {cleanup_failure}" if failure else cleanup_failure

    return {
        "status": "failed" if failure else "passed",
        "openai_requests": 0,
        "title": title,
        "spreadsheet_id": spreadsheet_id,
        "failure": failure,
        "receipts": receipts,
    }


def _read_test_row(
    *,
    spreadsheet_id: str,
    title: str,
    folder_path: str,
    row_key: str,
) -> dict[str, str]:
    result = google_sheet_read_table_impl(
        spreadsheet_id,
        title=title,
        folder_path=folder_path,
        sheet_name="Validation",
        max_rows=20,
        live=True,
    )
    rows = result.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return {}
    headers = [str(value) for value in rows[0]] if isinstance(rows[0], list) else []
    for values in rows[1:]:
        if not isinstance(values, list):
            continue
        record = {
            header: str(values[index]) if index < len(values) else ""
            for index, header in enumerate(headers)
        }
        if record.get("record_key") == row_key:
            return record
    return {}


def _write_receipt(result: dict[str, object]) -> dict[str, object]:
    receipt = {
        key: result.get(key)
        for key in (
            "status",
            "spreadsheet_id",
            "title",
            "sheet_name",
            "row_count",
            "row_number",
            "deleted_row_index",
            "updated_range",
            "trashed",
            "approval_reference",
            "send_enabled",
        )
        if key in result
    }
    verification = result.get("verification")
    if isinstance(verification, dict):
        receipt["verification"] = {
            key: verification.get(key)
            for key in (
                "status",
                "passed",
                "spreadsheet_id_match",
                "title_match",
                "mime_type_match",
                "trashed",
                "trashed_match",
                "row_count_match",
                "values_match",
                "verified_row_count",
                "row_found",
                "row_number",
                "matched_fields",
                "mismatched_fields",
                "row_count_delta",
                "row_absent",
            )
            if key in verification
        }
    return receipt


if __name__ == "__main__":
    raise SystemExit(main())
