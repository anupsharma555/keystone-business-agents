"""Run one bounded Google Doc create/read/update/read/trash lifecycle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import parse_bool, require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_doc_trash_impl,
    google_doc_write_impl,
    google_drive_get_file_metadata_impl,
)

GOOGLE_DOC_TEST_MARKER = "KBA_TEST_DOC"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder-path", default="KNIOps")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument(
        "--output",
        default="artifacts/test-pack/google-doc-test-lifecycle.json",
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
            live_action="creating, updating, verifying, and trashing one marked Google Doc",
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
    approval_reference = args.approval_reference.strip() or f"anu-199-doc-{suffix}"
    result = execute_google_doc_test_lifecycle(
        suffix=suffix,
        folder_path=args.folder_path,
        approval_reference=approval_reference,
        live=live,
    )
    _write_result_atomic(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "dry-run"} else 1


def execute_google_doc_test_lifecycle(
    *,
    suffix: str,
    folder_path: str,
    approval_reference: str,
    live: bool,
) -> dict[str, object]:
    title = f"{GOOGLE_DOC_TEST_MARKER}_{suffix}"
    original_body = f"{GOOGLE_DOC_TEST_MARKER} original content {suffix}."
    updated_body = f"{GOOGLE_DOC_TEST_MARKER} modified content {suffix}."
    document_id = ""
    receipts: dict[str, object] = {}
    failure = ""
    try:
        created = google_doc_write_impl(
            title,
            original_body,
            folder_path=folder_path,
            approval_reference=f"{approval_reference}:create",
            live=live,
        )
        receipts["create"] = _bounded_receipt(created)
        document_id = str(created.get("document_id") or "")
        if not live:
            return {
                "status": "dry-run",
                "openai_requests": 0,
                "title": title,
                "receipts": receipts,
            }
        if not document_id:
            raise RuntimeError("Google Doc create returned no document ID.")

        create_read = google_doc_read_impl(document_id, folder_path=folder_path, live=True)
        create_verified = _doc_read_matches(
            create_read,
            document_id=document_id,
            title=title,
            body=original_body,
        )
        receipts["create_readback"] = _doc_read_receipt(create_read, create_verified)
        if not create_verified:
            raise RuntimeError("Google Doc create read-back failed.")

        updated = google_doc_write_impl(
            title,
            updated_body,
            document_id=document_id,
            folder_path=folder_path,
            approval_reference=f"{approval_reference}:update",
            live=True,
        )
        receipts["update"] = _bounded_receipt(updated)
        update_read = google_doc_read_impl(document_id, folder_path=folder_path, live=True)
        update_verified = _doc_read_matches(
            update_read,
            document_id=document_id,
            title=title,
            body=updated_body,
        )
        receipts["update_readback"] = _doc_read_receipt(update_read, update_verified)
        if not update_verified:
            raise RuntimeError("Google Doc update read-back failed.")
    except Exception as exc:  # cleanup still runs below
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and document_id:
            try:
                trashed = google_doc_trash_impl(
                    document_id,
                    folder_path=folder_path,
                    approval_reference=f"{approval_reference}:trash",
                    live=True,
                )
                receipts["trash"] = _bounded_receipt(trashed)
                trash_metadata = google_drive_get_file_metadata_impl(
                    document_id,
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
                    failure = "RuntimeError: Google Doc trash read-back failed."
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {exc}"
                failure = f"{failure}; cleanup {cleanup_failure}" if failure else cleanup_failure

    return {
        "status": "failed" if failure else "passed",
        "openai_requests": 0,
        "title": title,
        "document_id": document_id,
        "failure": failure,
        "receipts": receipts,
    }


def _doc_read_matches(
    result: dict[str, object],
    *,
    document_id: str,
    title: str,
    body: str,
) -> bool:
    return bool(
        result.get("status") == "success"
        and result.get("document_id") == document_id
        and result.get("title") == title
        and str(result.get("text") or "").strip() == body
    )


def _doc_read_receipt(result: dict[str, object], passed: bool) -> dict[str, object]:
    return {
        "status": "verified" if passed else "verification_failed",
        "passed": passed,
        "document_id": result.get("document_id", ""),
        "title": result.get("title", ""),
        "char_count": result.get("char_count", 0),
        "truncated": bool(result.get("truncated")),
    }


def _bounded_receipt(result: dict[str, object]) -> dict[str, object]:
    receipt = {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "document_id",
            "title",
            "folder_path",
            "trashed",
            "approval_reference",
            "send_enabled",
        )
        if key in result
    }
    verification = result.get("verification")
    if isinstance(verification, dict):
        receipt["verification"] = verification
    return receipt


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
