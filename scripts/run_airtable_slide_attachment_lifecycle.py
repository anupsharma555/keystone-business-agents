"""Derive one slide and verify a reversible Airtable attachment lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_TEST_RECORD_MARKER,
    airtable_delete_test_record_impl,
    airtable_read_records_impl,
    airtable_upload_attachment_impl,
    airtable_write_record_impl,
    presentation_delete_test_artifact_local_impl,
    presentation_extract_slide_copy_local_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/airtable-slide-attachment-lifecycle.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relative_path")
    parser.add_argument("--slide-number", type=int, default=2)
    parser.add_argument("--base-alias", default="finance_tax_tracker")
    parser.add_argument("--table", default="Business Expenses")
    parser.add_argument("--attachment-field", default="Attachments")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-provider-write", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


def execute_airtable_slide_attachment_lifecycle(
    *,
    relative_path: str,
    slide_number: int,
    base_alias: str,
    table: str,
    attachment_field: str,
    suffix: str,
    approval_reference: str,
    live: bool,
) -> dict[str, object]:
    """Create, attach, update, and remove one marked record and local slide copy."""

    marker = f"{AIRTABLE_TEST_RECORD_MARKER} slide-{suffix}"
    artifact: dict[str, Any] = {}
    created: dict[str, Any] = {}
    attached: dict[str, Any] = {}
    updated: dict[str, Any] = {}
    record_cleanup: dict[str, Any] = {}
    artifact_cleanup: dict[str, Any] = {}
    orphan_check: dict[str, Any] = {}
    record_id = ""
    failure = ""
    cleanup_errors: list[str] = []
    try:
        artifact = presentation_extract_slide_copy_local_impl(
            relative_path,
            slide_number,
            output_format="png",
            output_name=f"KBA_TEST_SLIDE-{suffix}-airtable.png",
            approval_reference=f"{approval_reference}:derive",
            live=live,
        )
        artifact_path = str(artifact.get("artifact_path") or "")
        if live and not _verified(artifact):
            raise RuntimeError("Derived slide did not pass verification.")
        created = airtable_write_record_impl(
            json.dumps(
                {
                    "Item": marker,
                    "Description": f"{marker} derived-slide attachment validation",
                },
                sort_keys=True,
            ),
            table=table,
            base_alias=base_alias,
            approval_reference=f"{approval_reference}:create-record",
            operation="create",
            validate_schema=True,
            live=live,
        )
        record_id = str(created.get("record_id") or "")
        if not live:
            return _payload(
                "preview",
                artifact=artifact,
                created=created,
            )
        if not record_id or not _verified(created):
            raise RuntimeError("Marked Airtable record create did not verify.")
        attached = airtable_upload_attachment_impl(
            artifact_path,
            table=table,
            base_alias=base_alias,
            record_id=record_id,
            field_name=attachment_field,
            approval_reference=f"{approval_reference}:upload",
            live=True,
        )
        if not _verified(attached):
            raise RuntimeError("Airtable attachment upload did not pass provider read-back.")
        updated = airtable_write_record_impl(
            json.dumps(
                {"Description": f"{marker} modified after verified slide upload"},
                sort_keys=True,
            ),
            table=table,
            base_alias=base_alias,
            record_id=record_id,
            approval_reference=f"{approval_reference}:update-record",
            operation="update",
            validate_schema=True,
            live=True,
        )
        if not _verified(updated) or str(updated.get("record_id") or "") != record_id:
            raise RuntimeError("Airtable record update did not preserve and verify identity.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and not record_id:
            try:
                recovery = airtable_read_records_impl(
                    table,
                    base_alias=base_alias,
                    filter_formula=f'{{Item}}="{marker}"',
                    max_records=2,
                    live=True,
                )
                recovery_records = recovery.get("records", [])
                recovery_count = (
                    len(recovery_records) if isinstance(recovery_records, list) else 0
                )
                orphan_check = {
                    "status": "verified" if recovery_count <= 1 else "ambiguous",
                    "verification": {
                        "passed": recovery_count <= 1,
                        "matching_record_count": recovery_count,
                        "record_absent": recovery_count == 0,
                    },
                }
                if recovery_count == 1:
                    recovered_id = str(recovery_records[0].get("id") or "")
                    if not recovered_id:
                        raise RuntimeError("Recovered Airtable test record had no provider ID.")
                    record_id = recovered_id
                    orphan_check["recovered_record"] = True
                elif recovery_count > 1:
                    raise RuntimeError(
                        "Timed-out Airtable create resolved to multiple marked records."
                    )
            except Exception as exc:
                cleanup_errors.append(f"orphan check: {type(exc).__name__}: {exc}")
        if live and record_id:
            try:
                record_cleanup = airtable_delete_test_record_impl(
                    record_id,
                    table=table,
                    base_alias=base_alias,
                    approval_reference=f"{approval_reference}:delete-record",
                    live=True,
                )
            except Exception as exc:
                cleanup_errors.append(f"record cleanup: {type(exc).__name__}: {exc}")
        artifact_path = str(artifact.get("artifact_path") or "")
        if live and artifact_path:
            try:
                artifact_cleanup = presentation_delete_test_artifact_local_impl(
                    artifact_path,
                    approval_reference=f"{approval_reference}:delete-artifact",
                    live=True,
                )
            except Exception as exc:
                cleanup_errors.append(f"artifact cleanup: {type(exc).__name__}: {exc}")

    passed = bool(
        not failure
        and not cleanup_errors
        and all(
            _verified(result)
            for result in (
                artifact,
                created,
                attached,
                updated,
                record_cleanup,
                artifact_cleanup,
            )
        )
        and str(created.get("record_id") or "") == str(updated.get("record_id") or "")
    )
    return _payload(
        "passed" if passed else "failed",
        artifact=artifact,
        created=created,
        attached=attached,
        updated=updated,
        record_cleanup=record_cleanup,
        artifact_cleanup=artifact_cleanup,
        orphan_check=orphan_check,
        failure=failure,
        cleanup_errors=cleanup_errors,
    )


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    live = bool(args.live_provider_write and not args.dry_run)
    if args.live_provider_write:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-provider-write",
            live_action="uploading one derived slide to a marked Airtable record and cleaning up",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-provider-write.")
    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"airtable-slide-{suffix}"
    result = execute_airtable_slide_attachment_lifecycle(
        relative_path=args.relative_path,
        slide_number=args.slide_number,
        base_alias=args.base_alias,
        table=args.table,
        attachment_field=args.attachment_field,
        suffix=suffix,
        approval_reference=approval,
        live=live,
    )
    _write_receipt(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "preview"} else 1


def _verified(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _payload(
    status: str,
    *,
    artifact: dict[str, Any],
    created: dict[str, Any],
    attached: dict[str, Any] | None = None,
    updated: dict[str, Any] | None = None,
    record_cleanup: dict[str, Any] | None = None,
    artifact_cleanup: dict[str, Any] | None = None,
    orphan_check: dict[str, Any] | None = None,
    failure: str = "",
    cleanup_errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "openai_requests": 0,
        "live_search": False,
        "send_or_post": False,
        "artifact": _safe_receipt(artifact),
        "create": _safe_receipt(created),
        "attachment": _safe_receipt(attached or {}),
        "update": _safe_receipt(updated or {}),
        "record_cleanup": _safe_receipt(record_cleanup or {}),
        "artifact_cleanup": _safe_receipt(artifact_cleanup or {}),
        "orphan_check": _safe_receipt(orphan_check or {}),
        "failure": failure,
        "cleanup_errors": cleanup_errors or [],
    }


def _safe_receipt(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "operation",
        "record_id",
        "table",
        "field_name",
        "filename",
        "artifact_path",
        "derived_copy_created",
        "parent_modified",
        "verification",
        "recovered_record",
        "send_enabled",
    }
    return {key: value for key, value in result.items() if key in allowed}


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
