"""Bounded provider reads for Chief-owned multi-source context summaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from keystone_agents.schemas.chief_context import (
    ChiefContextEvidenceBundle,
    ChiefContextEvidenceItem,
    ChiefContextEvidenceReceipt,
)
from keystone_agents.schemas.work_item import WorkItemStatus
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema_impl,
    airtable_read_records_impl,
)

MAX_GMAIL_ITEMS = 5
MAX_AIRTABLE_TABLES = 5
MAX_AIRTABLE_RECORDS_PER_TABLE = 3
MAX_OPEN_WORK_ITEMS = 12
_SAFE_AIRTABLE_FIELD_TYPES = {
    "autoNumber",
    "checkbox",
    "count",
    "createdTime",
    "currency",
    "date",
    "dateTime",
    "duration",
    "email",
    "formula",
    "lastModifiedTime",
    "multilineText",
    "multipleSelects",
    "number",
    "percent",
    "phoneNumber",
    "rating",
    "richText",
    "rollup",
    "singleLineText",
    "singleSelect",
}
_BLOCKED_AIRTABLE_FIELD_NAME_PARTS = {
    "access token",
    "account number",
    "attachment",
    "bank account",
    "credential",
    "document",
    "file",
    "image",
    "oauth",
    "password",
    "receipt",
    "routing number",
    "secret",
    "url",
}


class WorkItemReader(Protocol):
    """Minimum local-state contract needed by Chief context acquisition."""

    def list_work_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[Any]: ...


def _compact_text(value: object, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _safe_airtable_field_name(value: object) -> bool:
    name = " ".join(str(value or "").strip().lower().split())
    return bool(
        name
        and not any(part in name for part in _BLOCKED_AIRTABLE_FIELD_NAME_PARTS)
    )


def _safe_airtable_value(value: object) -> tuple[bool, object]:
    if value is None or isinstance(value, bool | int | float):
        return True, value
    if isinstance(value, str):
        if "http://" in value.lower() or "https://" in value.lower():
            return False, ""
        return True, _compact_text(value, max_chars=240)
    if isinstance(value, list | tuple | set):
        projected: list[object] = []
        for item in list(value)[:8]:
            if isinstance(item, Mapping | list | tuple | set):
                return False, ""
            allowed, safe_item = _safe_airtable_value(item)
            if not allowed:
                return False, ""
            projected.append(safe_item)
        return True, projected
    return False, ""


def _safe_airtable_fields_for_table(table: Mapping[str, Any]) -> set[str]:
    raw_fields = table.get("fields")
    fields = raw_fields if isinstance(raw_fields, list) else []
    safe: set[str] = set()
    for raw_field in fields:
        field = raw_field if isinstance(raw_field, Mapping) else {}
        name = str(field.get("name") or "").strip()
        field_type = str(
            field.get("field_type") or field.get("type") or ""
        ).strip()
        if (
            name
            and _safe_airtable_field_name(name)
            and field_type in _SAFE_AIRTABLE_FIELD_TYPES
        ):
            safe.add(name)
    return safe


def _blocked_receipt(
    source: str,
    *,
    provider: str,
    operation: str,
    details: str,
    error: Exception | None = None,
    provider_read: bool = False,
) -> ChiefContextEvidenceReceipt:
    return ChiefContextEvidenceReceipt(
        source=source,
        provider=provider,
        operation=operation,
        status="blocked",
        verified=False,
        provider_read=provider_read,
        details=details,
        error_type=type(error).__name__ if error is not None else "",
    )


def _gmail_evidence(
    *,
    query: str,
    live: bool,
    gmail_tool: GmailTool | None,
) -> tuple[list[ChiefContextEvidenceItem], ChiefContextEvidenceReceipt]:
    if not live:
        return [], _blocked_receipt(
            "gmail",
            provider="google_gmail",
            operation="search_message_summaries",
            details="Live Gmail reads are not enabled for this command.",
        )
    if not query.strip():
        return [], _blocked_receipt(
            "gmail",
            provider="google_gmail",
            operation="search_message_summaries",
            details="The canonical plan did not provide a bounded Gmail query.",
        )
    tool = gmail_tool or GmailTool(live=live)
    try:
        summaries = tool.search_message_summaries(
            query=query,
            max_results=MAX_GMAIL_ITEMS,
        )
    except Exception as exc:
        return [], _blocked_receipt(
            "gmail",
            provider="google_gmail",
            operation="search_message_summaries",
            details="The bounded Gmail read failed before evidence was available.",
            error=exc,
            provider_read=True,
        )
    items: list[ChiefContextEvidenceItem] = []
    for index, raw_summary in enumerate(summaries[:MAX_GMAIL_ITEMS]):
        summary = raw_summary if isinstance(raw_summary, Mapping) else {}
        message_id = str(summary.get("id") or summary.get("message_id") or "").strip()
        thread_id = str(summary.get("thread_id") or summary.get("threadId") or "").strip()
        source_id = message_id or thread_id or f"gmail-result-{index + 1}"
        sender = str(
            summary.get("sender")
            or summary.get("sender_name")
            or summary.get("sender_email")
            or ""
        ).strip()
        subject = str(summary.get("subject") or "(no subject)").strip()
        items.append(
            ChiefContextEvidenceItem(
                source="gmail",
                source_id=source_id,
                title=subject,
                summary=str(summary.get("snippet") or ""),
                metadata={
                    "thread_id": thread_id,
                    "sender": _compact_text(sender, max_chars=160),
                    "received_at": _compact_text(
                        summary.get("received_at") or summary.get("date"),
                        max_chars=120,
                    ),
                },
            )
        )
    status = "success" if items else "empty"
    return items, ChiefContextEvidenceReceipt(
        source="gmail",
        provider="google_gmail",
        operation="search_message_summaries",
        status=status,
        verified=True,
        provider_read=True,
        item_count=len(items),
        query=query,
        evidence_ids=[item.source_id for item in items],
        details=(
            "Bounded Gmail metadata read completed."
            if items
            else "Bounded Gmail metadata read completed with zero matching messages."
        ),
    )


def _airtable_evidence(
    *,
    live: bool,
    schema_reader: Callable[..., dict[str, Any]],
    records_reader: Callable[..., dict[str, Any]],
) -> tuple[list[ChiefContextEvidenceItem], ChiefContextEvidenceReceipt]:
    try:
        schema_result = schema_reader(live=live)
    except Exception as exc:
        return [], _blocked_receipt(
            "airtable",
            provider="airtable",
            operation="schema_and_bounded_records",
            details="The Airtable schema read failed before a record scope could be selected.",
            error=exc,
            provider_read=True,
        )
    if str(schema_result.get("status") or "") != "success":
        return [], _blocked_receipt(
            "airtable",
            provider="airtable",
            operation="schema_and_bounded_records",
            details="The Airtable schema result was not a verified live provider read.",
            provider_read=bool(live),
        )
    schema = schema_result.get("schema")
    schema_payload = schema if isinstance(schema, Mapping) else {}
    allowed_tables = {
        str(item or "").strip()
        for item in (schema_payload.get("allowed_tables") or [])
        if str(item or "").strip()
    }
    table_payloads = [
        item
        for item in (schema_payload.get("tables") or [])
        if isinstance(item, Mapping)
    ]
    readable_tables = [
        str(item.get("name") or "").strip()
        for item in table_payloads
        if str(item.get("name") or "").strip()
        and (
            not allowed_tables
            or str(item.get("name") or "").strip() in allowed_tables
        )
    ][:MAX_AIRTABLE_TABLES]
    safe_fields_by_table = {
        str(table.get("name") or "").strip(): _safe_airtable_fields_for_table(table)
        for table in table_payloads
        if str(table.get("name") or "").strip()
    }
    if not readable_tables:
        return [], _blocked_receipt(
            "airtable",
            provider="airtable",
            operation="schema_and_bounded_records",
            details=(
                "Airtable schema was readable, but it exposed no approved table for "
                "a bounded record read."
            ),
            provider_read=True,
        )

    items: list[ChiefContextEvidenceItem] = []
    table_counts: list[str] = []
    for table in readable_tables:
        try:
            record_result = records_reader(
                table,
                max_records=MAX_AIRTABLE_RECORDS_PER_TABLE,
                live=live,
            )
        except Exception as exc:
            return items, _blocked_receipt(
                "airtable",
                provider="airtable",
                operation="schema_and_bounded_records",
                details=f"The bounded Airtable record read failed for table {table}.",
                error=exc,
                provider_read=True,
            )
        if str(record_result.get("status") or "") != "success":
            return items, _blocked_receipt(
                "airtable",
                provider="airtable",
                operation="schema_and_bounded_records",
                details=(
                    f"The bounded Airtable record result for table {table} was not "
                    "a verified live provider read."
                ),
                provider_read=True,
            )
        records = record_result.get("records")
        record_list = records if isinstance(records, list) else []
        table_counts.append(f"{table}:{len(record_list)}")
        for index, raw_record in enumerate(
            record_list[:MAX_AIRTABLE_RECORDS_PER_TABLE]
        ):
            record = raw_record if isinstance(raw_record, Mapping) else {}
            record_id = str(record.get("id") or "").strip() or (
                f"{table}-record-{index + 1}"
            )
            fields = record.get("fields")
            field_payload = fields if isinstance(fields, Mapping) else {}
            declared_safe_fields = safe_fields_by_table.get(table, set())
            projected_fields: dict[str, object] = {}
            for key, value in field_payload.items():
                field_name = str(key or "").strip()
                if not _safe_airtable_field_name(field_name):
                    continue
                if field_name not in declared_safe_fields:
                    continue
                allowed, projected_value = _safe_airtable_value(value)
                if not allowed:
                    continue
                projected_fields[_compact_text(field_name, max_chars=100)] = (
                    projected_value
                )
                if len(projected_fields) >= 8:
                    break
            primary_value = next(
                (
                    _compact_text(value, max_chars=180)
                    for value in projected_fields.values()
                    if isinstance(value, str) and value.strip()
                ),
                record_id,
            )
            items.append(
                ChiefContextEvidenceItem(
                    source="airtable",
                    source_id=record_id,
                    title=f"{table}: {primary_value}",
                    summary=f"Bounded record from Airtable table {table}.",
                    metadata={"table": table, "fields": projected_fields},
                )
            )
    status = "success" if items else "empty"
    return items, ChiefContextEvidenceReceipt(
        source="airtable",
        provider="airtable",
        operation="schema_and_bounded_records",
        status=status,
        verified=True,
        provider_read=True,
        item_count=len(items),
        evidence_ids=[item.source_id for item in items],
        details=(
            "Airtable schema and bounded record reads completed for "
            + ", ".join(table_counts)
            + "."
        ),
    )


def _work_item_evidence(
    *,
    store: WorkItemReader | None,
    current_work_item_id: str,
) -> tuple[list[ChiefContextEvidenceItem], ChiefContextEvidenceReceipt]:
    if store is None:
        return [], _blocked_receipt(
            "work_items",
            provider="local_sqlite",
            operation="list_open_work_items",
            details="The WorkItem store was unavailable for the requested local-state read.",
        )
    try:
        candidates = store.list_work_items(status="all", limit=MAX_OPEN_WORK_ITEMS + 1)
    except Exception as exc:
        return [], _blocked_receipt(
            "work_items",
            provider="local_sqlite",
            operation="list_open_work_items",
            details="The bounded local WorkItem read failed.",
            error=exc,
        )
    terminal = {WorkItemStatus.DONE, WorkItemStatus.ARCHIVED}
    selected = [
        item
        for item in candidates
        if str(getattr(item, "id", "")) != current_work_item_id
        and getattr(item, "status", None) not in terminal
    ][:MAX_OPEN_WORK_ITEMS]
    items = [
        ChiefContextEvidenceItem(
            source="work_items",
            source_id=str(item.id),
            title=str(item.title),
            summary=(
                str(item.next_action.description)
                if item.next_action is not None
                else str(item.request_text)
            ),
            metadata={
                "status": item.status.value,
                "route": item.current_route.value,
                "updated_at": item.updated_at,
                "open_blockers": [
                    blocker.code for blocker in item.blockers if not blocker.resolved
                ][:5],
            },
        )
        for item in selected
    ]
    status = "success" if items else "empty"
    return items, ChiefContextEvidenceReceipt(
        source="work_items",
        provider="local_sqlite",
        operation="list_open_work_items",
        status=status,
        verified=True,
        provider_read=True,
        item_count=len(items),
        evidence_ids=[item.source_id for item in items],
        details=(
            "Bounded open WorkItem read completed."
            if items
            else "Bounded open WorkItem read completed with zero open WorkItems."
        ),
    )


def acquire_chief_context_evidence(
    *,
    required_sources: Sequence[str],
    gmail_query: str = "",
    store: WorkItemReader | None = None,
    current_work_item_id: str = "",
    live: bool,
    gmail_live: bool | None = None,
    gmail_tool: GmailTool | None = None,
    airtable_schema_reader: Callable[..., dict[str, Any]] = (
        airtable_get_base_schema_impl
    ),
    airtable_records_reader: Callable[..., dict[str, Any]] = (
        airtable_read_records_impl
    ),
) -> ChiefContextEvidenceBundle:
    """Acquire every typed read obligation before one final Chief synthesis."""

    supported = {"gmail", "airtable", "work_items"}
    ordered_sources = list(
        dict.fromkeys(
            str(source or "").strip().lower()
            for source in required_sources
            if str(source or "").strip()
        )
    )
    unsupported = [source for source in ordered_sources if source not in supported]
    receipts: list[ChiefContextEvidenceReceipt] = []
    items: list[ChiefContextEvidenceItem] = []
    blockers = [
        f"Unsupported Chief context source: {source}."
        for source in unsupported
    ]
    for source in ordered_sources:
        if source == "gmail":
            source_items, receipt = _gmail_evidence(
                query=gmail_query,
                live=live if gmail_live is None else gmail_live,
                gmail_tool=gmail_tool,
            )
        elif source == "airtable":
            source_items, receipt = _airtable_evidence(
                live=live,
                schema_reader=airtable_schema_reader,
                records_reader=airtable_records_reader,
            )
        elif source == "work_items":
            source_items, receipt = _work_item_evidence(
                store=store,
                current_work_item_id=current_work_item_id,
            )
        else:
            continue
        items.extend(source_items)
        receipts.append(receipt)
        if not receipt.verified or receipt.status == "blocked":
            blockers.append(receipt.details or f"{source} context read was not verified.")
    complete = bool(ordered_sources) and not blockers and all(
        receipt.verified and receipt.status in {"success", "empty"}
        for receipt in receipts
    ) and len(receipts) == len(ordered_sources)
    return ChiefContextEvidenceBundle(
        required_sources=ordered_sources,
        receipts=receipts,
        items=items,
        complete=complete,
        blockers=blockers,
        live=live,
    )
