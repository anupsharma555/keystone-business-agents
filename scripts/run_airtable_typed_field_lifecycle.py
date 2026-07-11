#!/usr/bin/env python3
"""Run one schema-aware synthetic Airtable expense and receipt-link lifecycle."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.internal_data_tools import (
    AIRTABLE_TEST_RECORD_MARKER,
    airtable_delete_test_record_impl,
    airtable_link_attachment_impl,
    airtable_write_record_impl,
)

DEFAULT_RECEIPT_URL = (
    "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-alias", default="finance_tax_tracker")
    parser.add_argument("--table", default="Business Expenses")
    parser.add_argument("--receipt-url", default=DEFAULT_RECEIPT_URL)
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-airtable", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_airtable:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-airtable",
            live_action=(
                "creating, attaching to, modifying, and deleting one schema-aware "
                "marked Airtable expense record"
            ),
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-airtable.")

    suffix = uuid4().hex[:10]
    marker = f"{AIRTABLE_TEST_RECORD_MARKER} typed-{suffix}"
    approval = args.approval_reference.strip() or f"anu-198-typed-{suffix}"
    live = bool(args.live_airtable and not args.dry_run)
    create_fields = {
        "Item": marker,
        "Estimated Tax Periods": "3",
        "Date of Expense": "2026-07-10",
        "Categories": "Software",
        "Expense Client/Vendor": "Synthetic Vendor",
        "Description": f"{marker} created",
        "Amount": "19.99",
        "Receipt Available": False,
        "Payment Method": ["Business Debit Card (relay)"],
        "Additional Taxes": "1.60",
        "Total Expenses": "21.59",
    }
    update_fields = {
        "Item": marker,
        "Estimated Tax Periods": "4",
        "Date of Expense": "2026-07-11",
        "Categories": "Professional",
        "Expense Client/Vendor": "Synthetic Vendor Revised",
        "Description": f"{marker} modified",
        "Amount": "25.00",
        "Receipt Available": True,
        "Payment Method": ["Business Bank (relay)"],
        "Additional Taxes": "2.00",
        "Total Expenses": "27.00",
    }
    record_id = ""
    create_result: dict[str, object] = {}
    attachment_result: dict[str, object] = {}
    update_result: dict[str, object] = {}
    delete_result: dict[str, object] = {}
    try:
        create_result = airtable_write_record_impl(
            json.dumps(create_fields, sort_keys=True),
            table=args.table,
            base_alias=args.base_alias,
            approval_reference=f"{approval}:create",
            operation="create",
            validate_schema=True,
            live=live,
        )
        record_id = str(create_result.get("record_id") or "")
        if not live:
            print(json.dumps({"create": _receipt(create_result)}, indent=2, sort_keys=True))
            return 0
        if not record_id or not _verified(create_result):
            raise RuntimeError("Typed Airtable create did not pass read-back verification.")
        attachment_result = airtable_link_attachment_impl(
            args.receipt_url,
            table=args.table,
            base_alias=args.base_alias,
            record_id=record_id,
            field_name="Attachments",
            filename=f"{marker.replace(' ', '-')}-receipt.pdf",
            approval_reference=f"{approval}:attachment",
            live=True,
        )
        if not _verified(attachment_result):
            raise RuntimeError("Airtable receipt-link attachment did not verify.")
        update_result = airtable_write_record_impl(
            json.dumps(update_fields, sort_keys=True),
            table=args.table,
            base_alias=args.base_alias,
            record_id=record_id,
            approval_reference=f"{approval}:update",
            operation="update",
            validate_schema=True,
            live=True,
        )
        if not _verified(update_result):
            verification = update_result.get("verification", {})
            mismatched = (
                verification.get("mismatched_fields", [])
                if isinstance(verification, dict)
                else []
            )
            verified_record = update_result.get("verified_record", {})
            verified_fields = (
                verified_record.get("fields", {})
                if isinstance(verified_record, dict)
                else {}
            )
            checkbox_observed = (
                verified_fields.get("Receipt Available", "<omitted>")
                if isinstance(verified_fields, dict)
                else "<unavailable>"
            )
            provider_record = update_result.get("record", {})
            provider_fields = (
                provider_record.get("fields", {})
                if isinstance(provider_record, dict)
                else {}
            )
            provider_booleans = (
                {key: value for key, value in provider_fields.items() if isinstance(value, bool)}
                if isinstance(provider_fields, dict)
                else {}
            )
            raise RuntimeError(
                "Typed Airtable update did not pass read-back verification; "
                f"mismatched_fields={mismatched!r}; "
                f"receipt_available={checkbox_observed!r}; "
                f"provider_boolean_fields={provider_booleans!r}."
            )
    finally:
        if live and record_id:
            delete_result = airtable_delete_test_record_impl(
                record_id,
                table=args.table,
                base_alias=args.base_alias,
                approval_reference=f"{approval}:delete",
                live=True,
            )

    passed = all(
        _verified(result)
        for result in (create_result, attachment_result, update_result, delete_result)
    )
    print(
        json.dumps(
            {
                "status": "passed" if passed else "failed",
                "openai_requests": 0,
                "table": args.table,
                "record_id": record_id,
                "field_types_exercised": [
                    "multilineText",
                    "date",
                    "singleSelect",
                    "currency",
                    "checkbox",
                    "multipleSelects",
                    "multipleAttachments",
                ],
                "computed_fields_writeable": False,
                "create": _receipt(create_result),
                "attachment": _receipt(attachment_result),
                "update": _receipt(update_result),
                "delete": _receipt(delete_result),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


def _verified(result: dict[str, object]) -> bool:
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
            "field_name",
            "filename",
            "approval_reference",
            "schema_validation",
            "verification",
            "required_marker",
            "send_enabled",
        )
        if key in result
    }


if __name__ == "__main__":
    raise SystemExit(main())
