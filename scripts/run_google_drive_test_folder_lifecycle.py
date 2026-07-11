"""Run one bounded Google Drive folder create/read/rename/read/trash lifecycle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import parse_bool, require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    google_drive_create_folder_impl,
    google_drive_get_file_metadata_impl,
    google_drive_remove_folder_impl,
    google_drive_rename_folder_impl,
)

GOOGLE_DRIVE_TEST_FOLDER_MARKER = "KBA_TEST_FOLDER"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-folder", default="KNIOps")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument(
        "--output",
        default="artifacts/test-pack/google-drive-test-folder-lifecycle.json",
    )
    parser.add_argument("--live-google-workspace", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_google_workspace:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-google-workspace",
            live_action="creating, renaming, verifying, and trashing one marked Drive folder",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-google-workspace.")
    live = bool(args.live_google_workspace and not args.dry_run)
    if live and not parse_bool(os.getenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE")):
        raise SystemExit(
            "Live Google Workspace lifecycle is disabled. Set "
            "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true for the approved process."
        )

    suffix = uuid4().hex[:10]
    approval_reference = args.approval_reference.strip() or f"anu-199-folder-{suffix}"
    result = execute_google_drive_test_folder_lifecycle(
        suffix=suffix,
        parent_folder=args.parent_folder,
        approval_reference=approval_reference,
        live=live,
    )
    _write_result_atomic(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "dry-run"} else 1


def execute_google_drive_test_folder_lifecycle(
    *,
    suffix: str,
    parent_folder: str,
    approval_reference: str,
    live: bool,
) -> dict[str, object]:
    original_name = f"{GOOGLE_DRIVE_TEST_FOLDER_MARKER}_{suffix}"
    renamed_name = f"{GOOGLE_DRIVE_TEST_FOLDER_MARKER}_{suffix}_RENAMED"
    original_path = f"{parent_folder.rstrip('/')} / {original_name}"
    folder_id = ""
    current_name = original_name
    receipts: dict[str, object] = {}
    failure = ""
    try:
        created = google_drive_create_folder_impl(
            original_path,
            approval_reference=f"{approval_reference}:create",
            live=live,
        )
        receipts["create"] = _bounded_receipt(created)
        folder_id = str(created.get("folder_id") or "")
        if not live:
            return {
                "status": "dry-run",
                "openai_requests": 0,
                "original_name": original_name,
                "renamed_name": renamed_name,
                "receipts": receipts,
            }
        if not folder_id:
            raise RuntimeError("Google Drive folder create returned no folder ID.")

        create_metadata = google_drive_get_file_metadata_impl(
            folder_id,
            folder_path=parent_folder,
            live=True,
        )
        create_verified = _metadata_matches(
            create_metadata,
            expected_name=original_name,
            expected_trashed=False,
        )
        receipts["create_readback"] = _verification_receipt(
            create_verified,
            create_metadata,
        )
        if not create_verified:
            raise RuntimeError("Google Drive folder create read-back failed.")

        renamed = google_drive_rename_folder_impl(
            folder_id,
            renamed_name,
            approval_reference=f"{approval_reference}:rename",
            live=True,
        )
        current_name = renamed_name
        receipts["rename"] = _bounded_receipt(renamed)
        rename_metadata = google_drive_get_file_metadata_impl(
            folder_id,
            folder_path=parent_folder,
            live=True,
        )
        rename_verified = _metadata_matches(
            rename_metadata,
            expected_name=renamed_name,
            expected_trashed=False,
        )
        receipts["rename_readback"] = _verification_receipt(
            rename_verified,
            rename_metadata,
        )
        if not rename_verified:
            raise RuntimeError("Google Drive folder rename read-back failed.")
    except Exception as exc:  # cleanup still runs below
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and folder_id:
            try:
                removed = google_drive_remove_folder_impl(
                    folder_id,
                    approval_reference=f"{approval_reference}:trash",
                    live=True,
                )
                receipts["trash"] = _bounded_receipt(removed)
                trash_metadata = google_drive_get_file_metadata_impl(
                    folder_id,
                    folder_path=parent_folder,
                    live=True,
                )
                trash_verified = _metadata_matches(
                    trash_metadata,
                    expected_name=current_name,
                    expected_trashed=True,
                )
                receipts["trash_readback"] = _verification_receipt(
                    trash_verified,
                    trash_metadata,
                )
                if not trash_verified and not failure:
                    failure = "RuntimeError: Google Drive folder trash read-back failed."
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {exc}"
                failure = f"{failure}; cleanup {cleanup_failure}" if failure else cleanup_failure

    return {
        "status": "failed" if failure else "passed",
        "openai_requests": 0,
        "original_name": original_name,
        "renamed_name": renamed_name,
        "folder_id": folder_id,
        "failure": failure,
        "receipts": receipts,
    }


def _metadata_matches(
    metadata: dict[str, object],
    *,
    expected_name: str,
    expected_trashed: bool,
) -> bool:
    file_metadata = metadata.get("file")
    return bool(
        metadata.get("status") == "success"
        and isinstance(file_metadata, dict)
        and file_metadata.get("name") == expected_name
        and file_metadata.get("mime_type") == "application/vnd.google-apps.folder"
        and metadata.get("trashed") is expected_trashed
    )


def _verification_receipt(
    passed: bool,
    metadata: dict[str, object],
) -> dict[str, object]:
    file_metadata = metadata.get("file")
    return {
        "status": "verified" if passed else "verification_failed",
        "passed": passed,
        "name": file_metadata.get("name", "") if isinstance(file_metadata, dict) else "",
        "mime_type": (
            file_metadata.get("mime_type", "") if isinstance(file_metadata, dict) else ""
        ),
        "trashed": bool(metadata.get("trashed")),
    }


def _bounded_receipt(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "folder_id",
            "folder_path",
            "name",
            "trashed",
            "approval_reference",
            "send_enabled",
        )
        if key in result
    }


def _write_result_atomic(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
