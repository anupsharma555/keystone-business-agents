"""Run one bounded Airtable create/update/delete test-record lifecycle."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_TEST_RECORD_MARKER,
    airtable_delete_test_record_impl,
    airtable_write_record_impl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-alias", default="finance_tax_tracker")
    parser.add_argument("--table", default="Business Expenses")
    parser.add_argument("--label-field", default="Item")
    parser.add_argument("--text-field", default="Description")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-airtable", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_airtable:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-airtable",
            live_action="creating, modifying, and deleting one marked Airtable test record",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-airtable.")

    suffix = uuid4().hex[:10]
    marker_text = f"{AIRTABLE_TEST_RECORD_MARKER} {suffix}"
    approval_reference = args.approval_reference.strip() or f"anu-198-{suffix}"
    create_fields = {
        args.label_field: marker_text,
        args.text_field: f"{marker_text} created for lifecycle validation",
    }
    update_fields = {
        args.text_field: f"{marker_text} modified and ready for cleanup",
    }
    live = bool(args.live_airtable and not args.dry_run)
    record_id = ""
    create_result: dict[str, object] = {}
    update_result: dict[str, object] = {}
    delete_result: dict[str, object] = {}
    try:
        create_result = airtable_write_record_impl(
            json.dumps(create_fields),
            table=args.table,
            base_alias=args.base_alias,
            approval_reference=f"{approval_reference}:create",
            operation="create",
            live=live,
        )
        record_id = str(create_result.get("record_id") or "")
        if not live:
            print(json.dumps({"create": _receipt(create_result)}, indent=2, sort_keys=True))
            return 0
        if not record_id or not _verification_passed(create_result):
            raise RuntimeError("Airtable test create did not pass read-back verification.")

        update_result = airtable_write_record_impl(
            json.dumps(update_fields),
            table=args.table,
            base_alias=args.base_alias,
            record_id=record_id,
            approval_reference=f"{approval_reference}:update",
            operation="update",
            live=True,
        )
        if not _verification_passed(update_result):
            raise RuntimeError("Airtable test update did not pass read-back verification.")
    finally:
        if live and record_id:
            delete_result = airtable_delete_test_record_impl(
                record_id,
                table=args.table,
                base_alias=args.base_alias,
                approval_reference=f"{approval_reference}:delete",
                live=True,
            )

    result = {
        "status": (
            "passed"
            if _verification_passed(create_result)
            and _verification_passed(update_result)
            and _verification_passed(delete_result)
            else "failed"
        ),
        "openai_requests": 0,
        "table": args.table,
        "record_id": record_id,
        "create": _receipt(create_result),
        "update": _receipt(update_result),
        "delete": _receipt(delete_result),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def _verification_passed(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _receipt(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "table",
            "record_id",
            "approval_reference",
            "required_marker",
            "verification",
            "send_enabled",
            "audit_notes",
        )
        if key in result
    }


if __name__ == "__main__":
    raise SystemExit(main())
