"""Shared target inference for finance tracker receipt expense requests."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.local_file_inputs import read_supported_local_file

FINANCE_TAX_TRACKER_BASE_ALIAS = "finance_tax_tracker"
FINANCE_TAX_TRACKER_BASE_NAME = "2026 Finance & Tax Tracker"


@dataclass(frozen=True)
class FinanceExpenseReceiptTarget:
    """Resolved target for a receipt-backed finance tracker expense ask."""

    base_alias: str
    base_name: str
    table: str
    operation: str
    receipt_local_path: str = ""

    @property
    def receipt_filename(self) -> str:
        return Path(self.receipt_local_path).name if self.receipt_local_path else ""


@dataclass(frozen=True)
class FinanceReceiptEvidence:
    """Receipt facts extracted from an operator-supplied local artifact."""

    source_path: str
    filename: str
    content_read: bool
    extraction_method: str = ""
    blocker: str = ""
    vendor: str = ""
    receipt_date: str = ""
    order_number: str = ""
    description: str = ""
    quantity: str = ""
    subtotal: str = ""
    shipping: str = ""
    total: str = ""
    currency: str = ""
    payment_summary: str = ""
    estimated_tax_periods: str = ""
    text_excerpt: str = ""

    def supported_field_preview(self) -> dict[str, str]:
        """Return non-empty receipt-backed fields suitable for a write preview."""

        fields = {
            "Vendor or Merchant": self.vendor,
            "Description or Item": self.description,
            "Order or Receipt Number": self.order_number,
            "Date of Expense": self.receipt_date,
            "Estimated Tax Periods": self.estimated_tax_periods,
            "Amount": self.subtotal,
            "Shipping or Fees": self.shipping,
            "Total Expenses": self.total,
            "Payment Method": self.payment_summary,
            "Receipt or Attachments": self.filename,
        }
        return {key: value for key, value in fields.items() if value}

    def summary_fragment(self) -> str:
        if not self.content_read:
            return f"Receipt evidence was not read: {self.blocker or 'unavailable'}."
        facts = []
        if self.vendor:
            facts.append(f"vendor {self.vendor}")
        if self.receipt_date:
            facts.append(f"date {self.receipt_date}")
        if self.order_number:
            facts.append(f"order {self.order_number}")
        if self.total:
            total = f"{self.currency} {self.total}".strip()
            facts.append(f"total {total}")
        if self.estimated_tax_periods:
            facts.append(f"estimated tax period {self.estimated_tax_periods}")
        return "Receipt evidence read from artifact: " + "; ".join(facts) + "."


def infer_finance_expense_receipt_target(text: object) -> FinanceExpenseReceiptTarget | None:
    """Infer base/table target from an explicit Airtable expense receipt request."""

    raw_text = str(text or "")
    lowered = " ".join(raw_text.lower().split())
    names_finance_tracker = (
        "finance & tax tracker" in lowered
        or "finance and tax tracker" in lowered
        or "finance_tax_tracker" in lowered
    )
    if "airtable" not in lowered and not names_finance_tracker:
        return None
    has_receipt_evidence = (
        "receipt" in lowered
        or "invoice" in lowered
        or bool(_local_receipt_paths(raw_text))
    )
    if not has_receipt_evidence:
        return None
    table = ""
    if "business expense" in lowered or "business expenses" in lowered:
        table = "Business Expenses"
    elif "personal expense" in lowered or "personal expenses" in lowered:
        table = "Personal Expenses"
    if not table:
        return None
    if not re.search(
        r"\b(?:add|create|insert|record|update|change|correct|move|set|fill|remove|"
        r"verify|confirm|check|read)\b",
        lowered,
    ):
        return None
    operation = "create"
    if re.search(
        r"^(?:verify|confirm|check|read)\b|"
        r"\b(?:verify|confirm|check|read)\s+only\b|"
        r"\b(?:do\s+not|don't)\s+(?:modify|change|update)\b|"
        r"\bread[- ]only\b",
        lowered,
    ):
        operation = "read"
    elif re.search(r"\brec[a-zA-Z0-9]{10,}\b", raw_text) or re.search(
        r"\b(?:update|change|correct|move|set|relabel|replace|remove)\b",
        lowered,
    ):
        operation = "update"
    paths = _local_receipt_paths(raw_text)
    return FinanceExpenseReceiptTarget(
        base_alias=FINANCE_TAX_TRACKER_BASE_ALIAS,
        base_name=FINANCE_TAX_TRACKER_BASE_NAME,
        table=table,
        operation=operation,
        receipt_local_path=paths[0] if paths else "",
    )


def resolve_finance_expense_receipt_target(
    text: object,
    *,
    manual_plan: object | None = None,
) -> FinanceExpenseReceiptTarget | None:
    """Resolve a receipt target from the semantic plan, with heuristic fallback.

    A live LLM plan is authoritative for whether this is an Airtable expense
    operation and which expense table it targets. Raw text is used only to
    recover a selected local attachment path. The legacy phrase recognizer is
    retained solely for dry-run or planner-unavailable execution.
    """

    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.fallback_allowed:
        return infer_finance_expense_receipt_target(text)
    if authority.invalid:
        return None
    assert authority.plan is not None
    plan_model = authority.plan
    if plan_model.provider_system != "airtable":
        return None
    if plan_model.intent not in {
        "business_system_write",
        "context_lookup",
    }:
        return None

    plan = plan_model.model_dump(mode="python")

    table = _semantic_expense_table(plan)
    if not table:
        return None
    operations = list(authority.effective_provider_operations("airtable"))
    paths = _local_receipt_paths(str(text or ""))
    if not paths and "attach" not in operations:
        return None

    mutation_operations = [item for item in operations if item in {"create", "update"}]
    operation = (
        "update"
        if "update" in mutation_operations and "create" not in mutation_operations
        else "create"
        if "create" in mutation_operations
        else "read"
    )
    return FinanceExpenseReceiptTarget(
        base_alias=FINANCE_TAX_TRACKER_BASE_ALIAS,
        base_name=FINANCE_TAX_TRACKER_BASE_NAME,
        table=table,
        operation=operation,
        receipt_local_path=paths[0] if paths else "",
    )


def _manual_plan_mapping(value: object | None) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="python")
        if isinstance(payload, Mapping):
            return payload
    return {}


def _semantic_expense_table(plan: Mapping[str, object]) -> str:
    candidates = [
        str(plan.get("primary_target") or ""),
        *[
            str(item or "")
            for item in (plan.get("required_entities") or [])
            if str(item or "").strip()
        ],
    ]
    for candidate in candidates:
        normalized = re.sub(r"[^a-z0-9]+", " ", candidate.lower()).strip()
        if normalized == "business expenses" or normalized.endswith(" business expenses"):
            return "Business Expenses"
        if normalized == "personal expenses" or normalized.endswith(" personal expenses"):
            return "Personal Expenses"
    return ""


def finance_expense_receipt_provider_context(
    target: FinanceExpenseReceiptTarget,
) -> list[dict[str, str]]:
    """Return provider_call_context entries for Chief/Airtable specialist calls."""

    context = [
        {"key": "airtable_base_alias", "value": target.base_alias},
        {"key": "airtable_base_name", "value": target.base_name},
        {"key": "airtable_target_table", "value": target.table},
        {"key": "airtable_operation", "value": target.operation},
        {"key": "airtable_source_basis", "value": "operator-supplied receipt PDF/image"},
        {
            "key": "airtable_target_inference",
            "value": (
                "Airtable business/personal expense receipt asks map to the "
                "finance_tax_tracker base and the matching expense table."
            ),
        },
        {
            "key": "airtable_attachment_policy",
            "value": (
                "Attach only the operator-supplied receipt/invoice file after record "
                "creation and only if schema exposes a multipleAttachments field."
            ),
        },
    ]
    if target.receipt_local_path:
        context.append({"key": "receipt_local_path", "value": target.receipt_local_path})
    return context


def finance_expense_receipt_field_hints(target: FinanceExpenseReceiptTarget) -> list[str]:
    """Common field hints before live schema resolves exact Airtable names."""

    return [
        "Vendor or Merchant",
        "Description or Item",
        "Order or Receipt Number",
        "Date of Expense",
        "Estimated Tax Periods",
        "Amount",
        "Additional Taxes",
        "Shipping or Fees",
        "Total Expenses",
        "Payment Method",
        "Notes or Source",
        "Receipt or Attachments",
    ]


def match_receipt_evidence_to_airtable_fields(
    evidence: FinanceReceiptEvidence,
    schema_fields: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Map extracted receipt evidence onto exact Airtable schema field names.

    The matcher only returns fields that are both receipt-backed and safe for the
    configured Airtable field type. Ambiguous select values are surfaced as
    candidates/review notes instead of being written blindly.
    """

    fields_by_name = {
        str(field.get("name") or ""): field
        for field in schema_fields
        if isinstance(field, Mapping) and str(field.get("name") or "")
    }
    write_fields: dict[str, Any] = {}
    field_ids: dict[str, str] = {}
    review_notes: list[str] = []
    select_candidates: dict[str, list[str]] = {}
    unmapped_receipt_fields: dict[str, str] = {}

    def add_exact(field_name: str, value: object) -> None:
        if value in ("", None):
            return
        field = fields_by_name.get(field_name)
        if not field:
            unmapped_receipt_fields[field_name] = str(value)
            return
        if bool(field.get("is_computed")):
            review_notes.append(f"Skipped computed Airtable field `{field_name}`.")
            return
        mapped_value = _airtable_field_value(field, value)
        if mapped_value is None:
            review_notes.append(
                f"Skipped `{field_name}` because `{value}` is not safe for "
                f"{field.get('field_type') or 'unknown'}."
            )
            return
        write_fields[field_name] = mapped_value
        field_id = str(field.get("field_id") or "").strip()
        if field_id:
            field_ids[field_name] = field_id

    add_exact("Expense Client/Vendor", evidence.vendor)
    add_exact("Item", evidence.description)
    add_exact("Date of Expense", evidence.receipt_date)
    add_exact("Estimated Tax Periods", evidence.estimated_tax_periods)
    add_exact("Amount", evidence.subtotal)
    add_exact("Total Expenses", evidence.total)
    if evidence.filename:
        add_exact("Receipt Available", True)

    description_parts = []
    if evidence.description:
        description_parts.append(evidence.description)
    if evidence.order_number:
        description_parts.append(f"Order {evidence.order_number}")
    if evidence.quantity:
        description_parts.append(f"Quantity {evidence.quantity}")
    if evidence.shipping:
        description_parts.append(f"Shipping {evidence.shipping}")
    if evidence.payment_summary:
        description_parts.append(f"Paid with {evidence.payment_summary}")
    add_exact("Description", "; ".join(description_parts))

    payment_field = fields_by_name.get("Payment Method")
    if evidence.payment_summary and payment_field:
        candidates = _select_candidates_for_value(payment_field, evidence.payment_summary)
        if candidates:
            select_candidates["Payment Method"] = candidates
        review_notes.append(
            "`Payment Method` needs exact configured option review; the receipt says "
            f"`{evidence.payment_summary}`."
        )
    elif evidence.payment_summary:
        unmapped_receipt_fields["Payment Method"] = evidence.payment_summary

    if evidence.shipping and "Shipping or Fees" not in fields_by_name:
        unmapped_receipt_fields["Shipping or Fees"] = evidence.shipping
    if evidence.order_number and "Order or Receipt Number" not in fields_by_name:
        unmapped_receipt_fields["Order or Receipt Number"] = evidence.order_number

    attachment_field = _attachment_field(fields_by_name.values())
    if not attachment_field:
        review_notes.append("No Airtable multipleAttachments field was found for receipt upload.")

    if "Categories" in fields_by_name and evidence.description:
        candidates = _select_candidates_for_value(
            fields_by_name["Categories"],
            evidence.description,
        )
        if candidates:
            select_candidates["Categories"] = candidates
        review_notes.append(
            "`Categories` should be selected from configured Airtable options by the "
            "LLM/operator using the receipt item and business context."
        )

    return {
        "fields": write_fields,
        "field_ids": field_ids,
        "attachment_field": attachment_field,
        "select_candidates": select_candidates,
        "unmapped_receipt_fields": unmapped_receipt_fields,
        "review_notes": review_notes,
    }


def extract_finance_receipt_evidence(path: str | Path) -> FinanceReceiptEvidence:
    """Extract bounded receipt evidence from a supported local PDF/image."""

    source_path = str(path or "")
    try:
        local_file = read_supported_local_file(source_path)
    except (OSError, ValueError) as exc:
        return FinanceReceiptEvidence(
            source_path=source_path,
            filename=Path(source_path).name,
            content_read=False,
            blocker=f"{type(exc).__name__}: {exc}",
        )
    text, method, blocker = _extract_local_artifact_text(local_file.path)
    if not text.strip():
        return FinanceReceiptEvidence(
            source_path=str(local_file.path),
            filename=local_file.filename,
            content_read=False,
            extraction_method=method,
            blocker=blocker or "no text extracted",
        )
    parsed = parse_finance_receipt_text(text)
    return FinanceReceiptEvidence(
        source_path=str(local_file.path),
        filename=local_file.filename,
        content_read=True,
        extraction_method=method,
        vendor=parsed.get("vendor", ""),
        receipt_date=parsed.get("receipt_date", ""),
        order_number=parsed.get("order_number", ""),
        description=parsed.get("description", ""),
        quantity=parsed.get("quantity", ""),
        subtotal=parsed.get("subtotal", ""),
        shipping=parsed.get("shipping", ""),
        total=parsed.get("total", ""),
        currency=parsed.get("currency", ""),
        payment_summary=parsed.get("payment_summary", ""),
        estimated_tax_periods=parsed.get("estimated_tax_periods", ""),
        text_excerpt=_compact_text(text)[:1000],
    )


def parse_finance_receipt_text(text: str) -> dict[str, str]:
    """Parse common receipt/invoice fields from extracted artifact text."""

    normalized = _compact_text(text)
    fields: dict[str, str] = {}
    vendor = _vendor_from_text(text)
    if vendor:
        fields["vendor"] = vendor
    receipt_date = _receipt_date_from_text(text)
    if receipt_date:
        fields["receipt_date"] = receipt_date
        period = _estimated_tax_period_for_date(receipt_date)
        if period:
            fields["estimated_tax_periods"] = period
    order = _order_number_from_text(text)
    if order:
        fields["order_number"] = order
    description, quantity = _line_item_from_text(text)
    if description:
        fields["description"] = description
    if quantity:
        fields["quantity"] = quantity
    subtotal = _money_after_label(normalized, "Subtotal")
    if subtotal:
        fields["subtotal"] = subtotal
    shipping = _money_after_label(normalized, "Shipping")
    if shipping:
        fields["shipping"] = shipping
    total, currency = _total_from_text(normalized)
    if total:
        fields["total"] = total
    if currency:
        fields["currency"] = currency
    payment = _payment_summary_from_text(normalized)
    if payment:
        fields["payment_summary"] = payment
    return fields


def _local_receipt_paths(text: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"(?:~|/Users/|/private/|/tmp/)[^\s\"'<>]+?\.(?:pdf|png|jpe?g|webp|gif)",
        text,
        flags=re.I,
    ):
        path = match.group(0).rstrip(".,;:)")
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _extract_local_artifact_text(path: Path) -> tuple[str, str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdftotext = _resolve_local_executable(
            os.getenv("KEYSTONE_PDFTOTEXT_COMMAND", "pdftotext"),
            fallback_paths=(
                "/opt/homebrew/bin/pdftotext",
                "/usr/local/bin/pdftotext",
                "/usr/bin/pdftotext",
            ),
        )
        if pdftotext:
            try:
                completed = subprocess.run(
                    [pdftotext, "-layout", str(path), "-"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return "", "pdftotext", f"{type(exc).__name__}: {exc}"
            return completed.stdout, "pdftotext", ""
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            return "", "", "PDF text extraction requires pdftotext or optional pypdf"
        try:
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages), "pypdf", ""
        except Exception as exc:  # pragma: no cover - optional dependency fallback
            return "", "pypdf", f"{type(exc).__name__}: {exc}"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        tesseract = _resolve_local_executable(
            os.getenv("KEYSTONE_TESSERACT_COMMAND", "tesseract"),
            fallback_paths=(
                "/opt/homebrew/bin/tesseract",
                "/usr/local/bin/tesseract",
                "/usr/bin/tesseract",
            ),
        )
        if not tesseract:
            return "", "", "image OCR requires tesseract or live model image input"
        try:
            completed = subprocess.run(
                [tesseract, str(path), "stdout"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return "", "tesseract", f"{type(exc).__name__}: {exc}"
        return completed.stdout, "tesseract", ""
    return "", "", f"unsupported artifact extension {suffix}"


def _resolve_local_executable(
    command: str,
    *,
    fallback_paths: tuple[str, ...] = (),
) -> str:
    """Resolve a local helper even when a launch daemon has a minimal PATH."""

    candidate = str(command or "").strip()
    if candidate:
        if Path(candidate).is_absolute():
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        else:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
    for raw_path in fallback_paths:
        path = Path(raw_path).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return ""


def _vendor_from_text(text: str) -> str:
    lines = [" ".join(line.split()).strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not re.search(
            r"\b(?:thank\s+you\s+for\s+booking|booking\s+confirmed)\b",
            line,
            re.I,
        ):
            continue
        for candidate in lines[index + 1 : index + 5]:
            if candidate and not re.search(
                r"\b(?:address|check[- ]?in|check[- ]?out|confirmation|reservation)\b",
                candidate,
                re.I,
            ):
                return candidate[:120]
    for line in text.splitlines():
        clean = " ".join(line.split()).strip()
        if not clean:
            continue
        if re.search(
            r"\b(?:all\s+data\s+protected|no\s+unauthorized\s+use|"
            r"copyright|privacy\s+policy|terms\s+of\s+use)\b",
            clean,
            re.I,
        ):
            continue
        suffix = r"(?:Inc\.|Inc|LLC|Ltd\.|Ltd|Corporation|Corp\.|Corp|Company)"
        match = re.search(rf"\b([A-Z][A-Za-z0-9 &'.,-]{{1,80}}?\b{suffix})(?:\s|$)", clean)
        if match:
            vendor = match.group(1).strip()
            return re.sub(
                r"^(?:Receipt|Invoice)\s+from\s+",
                "",
                vendor,
                flags=re.I,
            ).strip()
    return ""


def _receipt_date_from_text(text: str) -> str:
    timestamp_match = re.search(
        r"^\s*(\d{1,2}/\d{1,2}/(?:\d{2}|\d{4}))\s*,?\s+"
        r"\d{1,2}:\d{2}\s*(?:AM|PM)\b",
        text,
        flags=re.I | re.M,
    )
    if timestamp_match:
        raw_timestamp_date = timestamp_match.group(1)
        for fmt in ("%m/%d/%y", "%m/%d/%Y"):
            try:
                return datetime.strptime(raw_timestamp_date, fmt).date().isoformat()
            except ValueError:
                continue
    match = re.search(
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+"
        r"\d{1,2},\s+\d{4}\b",
        text,
        flags=re.I,
    )
    if not match:
        return ""
    raw = match.group(0)
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _estimated_tax_period_for_date(date_value: str) -> str:
    try:
        parsed = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return ""
    if parsed.year != 2026:
        return ""
    if parsed.month <= 3:
        return "1"
    if parsed.month <= 5:
        return "2"
    if parsed.month <= 8:
        return "3"
    return "4"


def _line_item_from_text(text: str) -> tuple[str, str]:
    match = re.search(
        r"^\s*([A-Z][A-Z0-9 /&.-]{3,}?)\s+(\d{1,5})\s+\$?\d",
        text,
        flags=re.M,
    )
    if not match:
        return "", ""
    return " ".join(match.group(1).title().split()), match.group(2)


def _order_number_from_text(text: str) -> str:
    header = re.search(r"\bORDER\s+NO\.?\b", text, flags=re.I)
    if header:
        nearby = text[header.end() : header.end() + 400]
        for line in nearby.splitlines():
            candidates = re.findall(r"\b[A-Z0-9-]*\d{5,}[A-Z0-9-]*\b", line, flags=re.I)
            if candidates:
                return candidates[-1].strip()
    return _first_regex_group(
        text,
        (
            r"\b(?:order|invoice|receipt)\s*(?:no\.?|number|#)\s*:?\s*([A-Z0-9-]+)\b",
        ),
    )


def _money_after_label(text: str, label: str) -> str:
    match = re.search(rf"\b{re.escape(label)}\b\s+(?:USD\s+)?\$?([0-9][0-9,]*\.?\d*)", text, re.I)
    return _normalize_money(match.group(1)) if match else ""


def _total_from_text(text: str) -> tuple[str, str]:
    match = re.search(
        r"\b(?:Grand\s+Total|Amount\s+Paid|Total\s+Paid|Total)\b"
        r"\s+(?:(USD|CAD|EUR|GBP)\s+)?\$?([0-9][0-9,]*\.?\d*)",
        text,
        re.I,
    )
    if not match:
        return "", ""
    currency = (match.group(1) or "").upper()
    if not currency:
        currency_match = re.search(r"\b(USD|CAD|EUR|GBP)\b", text, re.I)
        currency = currency_match.group(1).upper() if currency_match else ""
    return _normalize_money(match.group(2)), currency


def _payment_summary_from_text(text: str) -> str:
    match = re.search(r"\bPaid with\s+(.{0,80}?)(?:$|\s{2,}| Have questions)", text, re.I)
    if not match:
        return ""
    return " ".join(match.group(1).split())


def _first_regex_group(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return str(match.group(1)).strip()
    return ""


def _normalize_money(value: str) -> str:
    try:
        amount = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return str(value).strip()
    return f"{amount:.2f}"


def _airtable_field_value(field: Mapping[str, Any], value: object) -> object | None:
    field_type = str(field.get("field_type") or "")
    if field_type in {"currency", "number", "percent"}:
        try:
            return float(Decimal(str(value).replace(",", "")))
        except InvalidOperation:
            return None
    if field_type == "checkbox":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y"}
    if field_type == "date":
        raw = str(value).strip()
        return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None
    if field_type == "multipleSelects":
        choices = _select_candidates_for_value(field, str(value))
        return choices or None
    if field_type == "singleSelect":
        choices = _select_candidates_for_value(field, str(value))
        return choices[0] if choices else None
    if field_type == "multipleAttachments":
        return None
    return str(value).strip()


def _select_candidates_for_value(field: Mapping[str, Any], value: str) -> list[str]:
    choices = [
        str(choice).strip()
        for choice in field.get("select_choices", [])
        if str(choice).strip()
    ]
    if not choices:
        return []
    normalized_value = _normalize_match_text(value)
    exact = [choice for choice in choices if _normalize_match_text(choice) == normalized_value]
    if exact:
        return exact
    value_words = set(normalized_value.split())
    candidates = [
        choice
        for choice in choices
        if value_words and value_words.issubset(set(_normalize_match_text(choice).split()))
    ]
    if candidates:
        return candidates
    if "credit card" in normalized_value:
        return [choice for choice in choices if "credit card" in _normalize_match_text(choice)]
    return []


def _attachment_field(fields: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    for field in fields:
        if str(field.get("field_type") or "") != "multipleAttachments":
            continue
        name = str(field.get("name") or "").strip()
        field_id = str(field.get("field_id") or "").strip()
        if name:
            return {"name": name, "field_id": field_id}
    return {}


def _normalize_match_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.lower()).split())


def _compact_text(text: str) -> str:
    return " ".join(str(text or "").split())
