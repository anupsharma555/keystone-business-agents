from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from keystone_agents.finance_expense_receipts import (
    _resolve_local_executable,
    extract_finance_receipt_evidence,
    finance_expense_receipt_field_hints,
    finance_expense_receipt_provider_context,
    infer_finance_expense_receipt_target,
    match_receipt_evidence_to_airtable_fields,
    parse_finance_receipt_text,
)


def test_resolve_local_executable_uses_absolute_fallback_when_path_is_minimal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    helper = tmp_path / "pdftotext"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o700)
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    assert _resolve_local_executable(
        "pdftotext",
        fallback_paths=(str(helper),),
    ) == str(helper)


def test_infer_business_expense_receipt_target_from_airtable_ask() -> None:
    target = infer_finance_expense_receipt_target(
        "add a business expense to the airtable business expenses based on "
        "the receipt details which are: "
        "/tmp/example-business-cards-receipt.pdf"
    )

    assert target is not None
    assert target.base_alias == "finance_tax_tracker"
    assert target.base_name == "2026 Finance & Tax Tracker"
    assert target.table == "Business Expenses"
    assert target.operation == "create"
    assert target.receipt_filename == "example-business-cards-receipt.pdf"

    context = {
        item["key"]: item["value"] for item in finance_expense_receipt_provider_context(target)
    }
    assert context["airtable_target_inference"].startswith(
        "Airtable business/personal expense receipt asks map"
    )
    assert context["airtable_target_table"] == "Business Expenses"
    assert context["receipt_local_path"].endswith("example-business-cards-receipt.pdf")
    assert "Estimated Tax Periods" in finance_expense_receipt_field_hints(target)


def test_infer_personal_expense_receipt_target_from_airtable_ask() -> None:
    target = infer_finance_expense_receipt_target(
        "create a personal expense in airtable personal expenses from "
        "/tmp/receipt.png"
    )

    assert target is not None
    assert target.base_alias == "finance_tax_tracker"
    assert target.table == "Personal Expenses"
    assert target.receipt_local_path == "/tmp/receipt.png"


def test_infer_expense_receipt_target_from_named_finance_tracker() -> None:
    target = infer_finance_expense_receipt_target(
        "Create one Business Expenses record in the configured Finance & Tax Tracker "
        "and link this receipt: https://example.test/receipt.pdf"
    )

    assert target is not None
    assert target.base_alias == "finance_tax_tracker"
    assert target.table == "Business Expenses"


def test_finance_receipt_target_requires_airtable_expense_and_evidence() -> None:
    assert infer_finance_expense_receipt_target("add a business expense from lunch") is None
    assert infer_finance_expense_receipt_target("read airtable business expenses") is None


def test_parse_finance_receipt_text_extracts_example_print_fields() -> None:
    parsed = parse_finance_receipt_text(
        """
        Order Confirmation
        Jun 28, 2026

        ORDER NO.
        1002003

        ITEM QTY PRICE
        BUSINESS CARDS 50 $31

        Example Print Inc.
        Subtotal $31
        Shipping $45.80
        Total USD $76.80
        PAYMENT INFO
        Paid with credit card ending in 0000
        """
    )

    assert parsed["vendor"] == "Example Print Inc."
    assert parsed["receipt_date"] == "2026-06-28"
    assert parsed["estimated_tax_periods"] == "3"
    assert parsed["order_number"] == "1002003"
    assert parsed["description"] == "Business Cards"
    assert parsed["quantity"] == "50"
    assert parsed["subtotal"] == "31.00"
    assert parsed["shipping"] == "45.80"
    assert parsed["total"] == "76.80"
    assert parsed["currency"] == "USD"
    assert parsed["payment_summary"] == "credit card ending in 0000"


def test_extract_finance_receipt_evidence_reads_supplied_example_print_pdf() -> None:
    receipt_path = Path("/tmp/example-business-cards-receipt.pdf")
    if not receipt_path.exists() or shutil.which("pdftotext") is None:
        pytest.skip("local Example Print receipt PDF or pdftotext is unavailable")

    evidence = extract_finance_receipt_evidence(receipt_path)

    assert evidence.content_read is True
    assert evidence.extraction_method == "pdftotext"
    assert evidence.vendor == "Example Print Inc."
    assert evidence.receipt_date == "2026-06-28"
    assert evidence.estimated_tax_periods == "3"
    assert evidence.order_number == "1002003"
    assert evidence.total == "76.80"
    assert evidence.currency == "USD"
    assert evidence.supported_field_preview()["Total Expenses"] == "76.80"


def test_match_receipt_evidence_to_airtable_fields_uses_exact_schema() -> None:
    evidence = extract_finance_receipt_evidence(
        "/tmp/example-business-cards-receipt.pdf"
    )
    if not evidence.content_read:
        parsed = parse_finance_receipt_text(
            """
            Jun 28, 2026
            ORDER NO.
            1002003
            BUSINESS CARDS 50 $31
            Example Print Inc.
            Subtotal $31
            Shipping $45.80
            Total USD $76.80
            Paid with credit card ending in 0000
            """
        )
        evidence = evidence.__class__(
            source_path="/tmp/receipt.pdf",
            filename="receipt.pdf",
            content_read=True,
            vendor=parsed["vendor"],
            receipt_date=parsed["receipt_date"],
            order_number=parsed["order_number"],
            description=parsed["description"],
            quantity=parsed["quantity"],
            subtotal=parsed["subtotal"],
            shipping=parsed["shipping"],
            total=parsed["total"],
            currency=parsed["currency"],
            payment_summary=parsed["payment_summary"],
            estimated_tax_periods=parsed["estimated_tax_periods"],
        )

    schema_fields = [
        {"name": "Item", "field_type": "multilineText", "field_id": "fldCoWHTa87Djd2yI"},
        {
            "name": "Estimated Tax Periods",
            "field_type": "multilineText",
            "field_id": "fld0xvGeviEdGZHXu",
        },
        {"name": "Date of Expense", "field_type": "date", "field_id": "fldh7Ytfs4kjmELEM"},
        {
            "name": "Categories",
            "field_type": "singleSelect",
            "field_id": "fldbnyaQvZBCtVaCK",
            "select_choices": [
                "Todo",
                "Insurance",
                "Done",
                "Software",
                "Condo",
                "Subscription",
                "Supplies",
                "Books",
                "Professional",
                "Formation/Maintenance",
            ],
        },
        {
            "name": "Expense Client/Vendor",
            "field_type": "multilineText",
            "field_id": "fldLAegBqE71DcgTp",
        },
        {"name": "Description", "field_type": "multilineText", "field_id": "fldEu6dO89poGCgA7"},
        {"name": "Amount", "field_type": "currency", "field_id": "fldN0YKCncbRFpq80"},
        {"name": "Receipt Available", "field_type": "checkbox", "field_id": "fldsYpls7cWu7DqhM"},
        {
            "name": "Payment Method",
            "field_type": "multipleSelects",
            "field_id": "fld7gIOO36UjeZQW1",
            "select_choices": [
                "Credit card (personal)",
                "Cash (personal)",
                "Check (personal)",
                "Bank (personal)",
                "Business Debit Card (relay)",
                "Business Bank (relay)",
            ],
        },
        {"name": "Additional Taxes", "field_type": "currency", "field_id": "fldjFHFEVeyA9FPVm"},
        {"name": "Total Expenses", "field_type": "currency", "field_id": "fldXvUqX9yXQQ9l2B"},
        {
            "name": "Attachments",
            "field_type": "multipleAttachments",
            "field_id": "fldpT1bIw45DIkm68",
        },
    ]

    mapping = match_receipt_evidence_to_airtable_fields(evidence, schema_fields)

    assert mapping["fields"]["Expense Client/Vendor"] == "Example Print Inc."
    assert mapping["fields"]["Item"] == "Business Cards"
    assert mapping["fields"]["Date of Expense"] == "2026-06-28"
    assert mapping["fields"]["Estimated Tax Periods"] == "3"
    assert mapping["fields"]["Amount"] == 31.0
    assert mapping["fields"]["Total Expenses"] == 76.8
    assert mapping["fields"]["Receipt Available"] is True
    assert "Shipping 45.80" in mapping["fields"]["Description"]
    assert mapping["attachment_field"] == {
        "name": "Attachments",
        "field_id": "fldpT1bIw45DIkm68",
    }
    assert mapping["field_ids"]["Total Expenses"] == "fldXvUqX9yXQQ9l2B"
    assert mapping["select_candidates"]["Payment Method"] == ["Credit card (personal)"]
    assert "Payment Method" not in mapping["fields"]
    assert mapping["unmapped_receipt_fields"]["Shipping or Fees"] == "45.80"
    assert any("Payment Method" in note for note in mapping["review_notes"])


def test_receipt_correction_is_an_update_not_a_create() -> None:
    target = infer_finance_expense_receipt_target(
        "Correct the Airtable Personal Expenses receipt record "
        "recReceiptKeep123 and move Estimated Tax Periods from Q3 to 3."
    )

    assert target is not None
    assert target.table == "Personal Expenses"
    assert target.operation == "update"


def test_receipt_cleanup_verification_is_read_only_despite_record_ids() -> None:
    target = infer_finance_expense_receipt_target(
        "Verify the Airtable Personal Expenses receipt cleanup only; do not modify "
        "anything. Confirm recReceiptKeep123 retains receipt.pdf and that "
        "recReceiptDuplicate456 is absent."
    )

    assert target is not None
    assert target.operation == "read"


def test_receipt_update_with_no_create_constraint_remains_an_update() -> None:
    target = infer_finance_expense_receipt_target(
        "Update Airtable Personal Expenses record recReceiptKeep123. Set Item to "
        "Linear Basic and preserve receipt.pdf. Do not create any record or field "
        "value. Verify the same ID."
    )

    assert target is not None
    assert target.operation == "update"
