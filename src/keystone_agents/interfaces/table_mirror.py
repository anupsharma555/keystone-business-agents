"""Provider-neutral table mirror adapters.

The dry-run provider is complete and deterministic. Live providers are
credential-gated placeholders and make no API calls.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from keystone_agents.presentation.renderers import (
    safe_export_text,
    sensitive_text_summary,
)
from keystone_agents.schemas.approval import ApprovalState, normalize_approval_state
from keystone_agents.schemas.contact_context import (
    ContactRecord,
    CRMAccountContext,
    LocalAccountContext,
)
from keystone_agents.schemas.crm import (
    CRMLeadRecord,
    CRMProviderName,
    CRMWriteOperation,
    CRMWriteResult,
    CRMWriteStatus,
    normalize_crm_provider_name,
)
from keystone_agents.schemas.table_mirror import (
    TableCellValue,
    TableMirrorExportResult,
    TableMirrorObjectType,
    TableMirrorProviderName,
    TableMirrorRecord,
    column_mappings_for_object_type,
    normalize_table_mirror_object_type,
    normalize_table_mirror_provider,
    table_name_for_object_type,
)


class TableMirrorProvider(Protocol):
    """Provider-neutral mirror adapter contract."""

    name: TableMirrorProviderName
    live: bool

    def export_records(
        self,
        object_type: TableMirrorObjectType | str,
        records: Sequence[Any],
    ) -> TableMirrorExportResult:
        """Mirror records into a provider-specific table or return a dry-run preview."""


def _as_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    if isinstance(record, Mapping):
        return dict(record)
    raise TypeError(f"Expected Pydantic model or mapping, got {type(record).__name__}.")


def _cell_value(value: Any) -> TableCellValue:
    if isinstance(value, TableMirrorProviderName | TableMirrorObjectType):
        return value.value
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple | set):
        simple_values = [_cell_value(item) for item in value]
        if all(
            isinstance(item, str | int | float | bool) or item is None for item in simple_values
        ):
            return ", ".join("" if item is None else str(item) for item in simple_values)
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return str(value)


def _safe_cell_value(source_field: str, value: Any) -> TableCellValue:
    if source_field.lower() in {
        "body",
        "email_body",
        "draft_text",
        "draft_reply",
        "raw_body",
        "message_body",
        "full_text",
        "content",
    }:
        return sensitive_text_summary(value)
    cell = _cell_value(value)
    if isinstance(cell, str):
        return safe_export_text(cell, max_chars=240)
    return cell


def _nested_values(data: Any, path_parts: list[str]) -> list[Any]:
    if not path_parts:
        return [data]
    current = path_parts[0]
    remainder = path_parts[1:]
    if isinstance(data, list):
        values: list[Any] = []
        for item in data:
            values.extend(_nested_values(item, path_parts))
        return values
    if isinstance(data, Mapping):
        if current not in data:
            return []
        return _nested_values(data[current], remainder)
    return []


def _field_value(data: dict[str, Any], source_field: str) -> TableCellValue:
    values = _nested_values(data, source_field.split("."))
    if not values:
        return None
    if len(values) == 1:
        return _safe_cell_value(source_field, values[0])
    return _safe_cell_value(source_field, values)


def _record_id(object_type: TableMirrorObjectType, data: dict[str, Any], index: int) -> str:
    candidates = {
        TableMirrorObjectType.OPPORTUNITIES: (
            data.get("company_name"),
            data.get("target_company"),
            data.get("title"),
        ),
        TableMirrorObjectType.COMPANIES: (
            data.get("name"),
            data.get("company_name"),
            data.get("website"),
        ),
        TableMirrorObjectType.DRAFTS: (
            data.get("draft_id"),
            data.get("email_subject"),
            data.get("company_name"),
        ),
        TableMirrorObjectType.APPROVALS: (
            data.get("id"),
            data.get("object_id"),
            data.get("summary"),
            data.get("timestamp"),
        ),
        TableMirrorObjectType.FEEDBACK: (
            data.get("id"),
            data.get("object_id"),
            data.get("rating"),
        ),
        TableMirrorObjectType.AUDIT_RECORDS: (
            data.get("id"),
            data.get("agent_name"),
            data.get("input_hash"),
        ),
        TableMirrorObjectType.CONTACTS: (
            data.get("id"),
            data.get("source_id"),
            data.get("contact_email"),
            data.get("recipient_email"),
            data.get("contact_name"),
            data.get("recipient"),
        ),
        TableMirrorObjectType.CRM_CONTEXTS: (
            data.get("company_name"),
            data.get("source_id"),
            data.get("account_stage"),
        ),
        TableMirrorObjectType.OUTREACH_TRACKING: (
            data.get("id"),
            data.get("draft_id"),
            data.get("company_name"),
            data.get("lifecycle_status"),
        ),
    }[object_type]
    for candidate in candidates:
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return f"{object_type.value}-{index}"


def _source_model(record: Any) -> str:
    if isinstance(record, BaseModel):
        return record.__class__.__name__
    return "mapping"


def table_record_from_source(
    *,
    object_type: TableMirrorObjectType | str,
    record: Any,
    index: int = 1,
) -> TableMirrorRecord:
    """Normalize a domain object into a provider-neutral table row."""

    resolved_type = normalize_table_mirror_object_type(object_type)
    data = _as_dict(record)
    fields = {
        mapping.column_name: _field_value(data, mapping.source_field)
        for mapping in column_mappings_for_object_type(resolved_type)
    }
    return TableMirrorRecord(
        object_type=resolved_type,
        record_id=_record_id(resolved_type, data, index),
        fields=fields,
        source_model=_source_model(record),
        source_ref=str(data.get("source_id") or data.get("source") or ""),
    )


@dataclass(frozen=True)
class DryRunTableMirrorProvider:
    """Deterministic table mirror provider that never calls external APIs."""

    name: TableMirrorProviderName = TableMirrorProviderName.DRY_RUN
    live: bool = False

    def export_records(
        self,
        object_type: TableMirrorObjectType | str,
        records: Sequence[Any],
    ) -> TableMirrorExportResult:
        resolved_type = normalize_table_mirror_object_type(object_type)
        mirrored_records = [
            table_record_from_source(object_type=resolved_type, record=record, index=index)
            for index, record in enumerate(records, start=1)
        ]
        return TableMirrorExportResult(
            provider=self.name,
            object_type=resolved_type,
            table_name=table_name_for_object_type(resolved_type),
            dry_run=True,
            live_provider_requested=False,
            columns=column_mappings_for_object_type(resolved_type),
            records=mirrored_records,
            audit_notes=[
                "Dry-run table mirror only; no live provider APIs were called.",
                "Rows show the exact field-to-column mapping for manual review.",
            ],
        )


def _coerce_crm_lead(record: Any) -> CRMLeadRecord:
    if isinstance(record, CRMLeadRecord):
        return record
    if isinstance(record, ContactRecord):
        return CRMLeadRecord.from_contact(record)
    data = _as_dict(record)
    try:
        return CRMLeadRecord.from_contact(ContactRecord.model_validate(data))
    except ValueError:
        return CRMLeadRecord.model_validate(data)


def _coerce_contact_record(record: Any) -> ContactRecord | None:
    if isinstance(record, ContactRecord):
        return record
    if isinstance(record, CRMLeadRecord):
        lead = record
    else:
        data = _as_dict(record)
        try:
            return ContactRecord.model_validate(data)
        except ValueError:
            lead = CRMLeadRecord.model_validate(data)
    if not lead.contact_name:
        return None
    return ContactRecord(
        company_name=lead.company_name,
        contact_name=lead.contact_name,
        role_title=lead.role_title,
        contact_email=lead.contact_email,
        linkedin_url=lead.linkedin_url,
        source=lead.source,
        source_id=lead.source_id,
        source_url=lead.source_url,
        confidence=lead.confidence,
        approval_state=lead.approval_state,
        approval_scope=lead.approval_scope,
        notes=lead.notes,
        claims=lead.claims,
        unsupported_claims_flagged=lead.unsupported_claims_flagged,
    )


def _coerce_crm_account_context(record: Any) -> CRMAccountContext:
    if isinstance(record, CRMAccountContext):
        return record
    return CRMAccountContext.model_validate(_as_dict(record))


def _matches_company(value: str, company_name: str | None) -> bool:
    if company_name is None or not company_name.strip():
        return True
    return value.strip().casefold() == company_name.strip().casefold()


def _append_crm_note(
    existing_note: str,
    *,
    report_link: str | None = None,
    note: str = "",
) -> str:
    parts = [existing_note.strip()] if existing_note.strip() else []
    if report_link:
        parts.append(f"Report link: {report_link.strip()}")
    if note.strip():
        parts.append(note.strip())
    return "\n".join(parts)


@dataclass(frozen=True)
class LocalTableMirrorCRMProvider:
    """Local fixture/table-mirror CRM provider with no live CRM calls."""

    leads: Sequence[Any] = ()
    account_contexts: Sequence[Any] = ()
    dry_run: bool = True
    name: CRMProviderName = CRMProviderName.LOCAL_TABLE_MIRROR
    table_mirror_provider: TableMirrorProvider = field(default_factory=DryRunTableMirrorProvider)

    def list_leads(
        self,
        company_name: str | None = None,
        *,
        approved_only: bool = False,
    ) -> list[CRMLeadRecord]:
        """List local fixture or table-mirror lead records."""

        leads = [
            _coerce_crm_lead(record)
            for record in self.leads
            if _matches_company(_coerce_crm_lead(record).company_name, company_name)
        ]
        if approved_only:
            return [lead for lead in leads if lead.approved_for_crm_write]
        return leads

    def update_status(
        self,
        lead_id: str,
        status: str,
        *,
        approval_state: ApprovalState | str = ApprovalState.PENDING,
        reviewer: str = "",
        note: str = "",
    ) -> CRMWriteResult:
        """Return a dry-run status update intent for a local lead."""

        requested_status = status.strip()
        if not requested_status:
            raise ValueError("CRM status update requires a non-empty status.")
        lead = self._lead_by_id(lead_id)
        return self._write_result(
            operation=CRMWriteOperation.UPDATE_STATUS,
            lead=lead,
            approval_state=approval_state,
            reviewer=reviewer,
            requested_status=requested_status,
            note=note,
            preview_lead=lead.model_copy(
                update={
                    "status": requested_status,
                    "notes": _append_crm_note(lead.notes, note=note),
                }
            ),
        )

    def attach_report_link_or_note(
        self,
        lead_id: str,
        *,
        report_link: str | None = None,
        note: str = "",
        approval_state: ApprovalState | str = ApprovalState.PENDING,
        reviewer: str = "",
    ) -> CRMWriteResult:
        """Return a dry-run report-link or note attachment intent for a local lead."""

        clean_report_link = report_link.strip() if report_link else None
        clean_note = note.strip()
        if not clean_report_link and not clean_note:
            raise ValueError("CRM attachment requires a report_link or note.")
        lead = self._lead_by_id(lead_id)
        return self._write_result(
            operation=CRMWriteOperation.ATTACH_REPORT_LINK_OR_NOTE,
            lead=lead,
            approval_state=approval_state,
            reviewer=reviewer,
            report_link=clean_report_link,
            note=clean_note,
            preview_lead=lead.model_copy(
                update={
                    "notes": _append_crm_note(
                        lead.notes,
                        report_link=clean_report_link,
                        note=clean_note,
                    )
                }
            ),
        )

    def fetch_account_context(
        self,
        company_name: str,
        *,
        approved_only: bool = True,
    ) -> LocalAccountContext:
        """Return local account context for a company without live CRM calls."""

        contacts = [
            contact
            for contact in (_coerce_contact_record(record) for record in self.leads)
            if contact is not None
            and _matches_company(contact.company_name, company_name)
            and (not approved_only or contact.approved_for_personalization)
        ]
        contexts = [
            context
            for context in (_coerce_crm_account_context(record) for record in self.account_contexts)
            if _matches_company(context.company_name, company_name)
            and (not approved_only or context.approved_for_personalization)
        ]
        return LocalAccountContext(
            company_name=company_name,
            contacts=contacts,
            crm_context=contexts[0] if contexts else None,
        )

    def _lead_by_id(self, lead_id: str) -> CRMLeadRecord:
        clean_id = lead_id.strip()
        if not clean_id:
            raise ValueError("CRM lead_id is required.")
        for lead in (_coerce_crm_lead(record) for record in self.leads):
            if clean_id in {lead.id, lead.source_id}:
                return lead
        raise ValueError(f"Local CRM lead not found: {lead_id}")

    def _write_result(
        self,
        *,
        operation: CRMWriteOperation,
        lead: CRMLeadRecord,
        approval_state: ApprovalState | str,
        reviewer: str,
        preview_lead: CRMLeadRecord,
        requested_status: str | None = None,
        report_link: str | None = None,
        note: str = "",
    ) -> CRMWriteResult:
        if not self.dry_run:
            raise RuntimeError(
                "Live CRM integrations are not implemented. Use dry_run=True for "
                "local table-mirror previews."
            )
        resolved_approval = normalize_approval_state(approval_state)
        approval_required = resolved_approval != ApprovalState.APPROVED_FOR_DRAFTING
        audit_notes = [
            "Local table-mirror CRM provider only; no live CRM API was called.",
            "Approved does not sync to HubSpot, Airtable, Google Sheets, or any CRM.",
        ]
        table_preview = None
        status = CRMWriteStatus.BLOCKED_PENDING_APPROVAL
        if approval_required:
            audit_notes.append(
                "Approval for drafting is required before this CRM update can be used."
            )
        else:
            status = CRMWriteStatus.DRY_RUN_READY
            table_preview = self._preview_lead(preview_lead)
            audit_notes.append(
                "Approval was supplied, but this remains a dry-run preview for manual review."
            )
        return CRMWriteResult(
            provider=self.name,
            operation=operation,
            object_id=lead.id,
            company_name=lead.company_name,
            dry_run=True,
            approval_required=approval_required,
            approval_state=resolved_approval,
            reviewer=reviewer,
            status=status,
            requested_status=requested_status,
            report_link=report_link,
            note=note,
            table_preview=table_preview,
            audit_notes=audit_notes,
        )

    def _preview_lead(self, lead: CRMLeadRecord) -> TableMirrorExportResult:
        if getattr(self.table_mirror_provider, "live", False):
            raise RuntimeError("CRM provider supports dry-run table mirror previews only.")
        return self.table_mirror_provider.export_records(TableMirrorObjectType.CONTACTS, [lead])


@dataclass(frozen=True)
class AirtableMirrorProvider:
    """Placeholder Airtable adapter. No live API calls are implemented."""

    api_key: str | None = None
    base_id: str | None = None
    table_name: str | None = None
    live: bool = False
    name: TableMirrorProviderName = TableMirrorProviderName.AIRTABLE

    def export_records(
        self,
        object_type: TableMirrorObjectType | str,
        records: Sequence[Any],
    ) -> TableMirrorExportResult:
        _ = records
        if not self.live:
            raise RuntimeError(
                "Airtable mirror requires explicit live=True. No live API call was made."
            )
        api_key = self.api_key or os.getenv("AIRTABLE_API_KEY")
        base_id = self.base_id or os.getenv("AIRTABLE_BASE_ID")
        if not api_key or not base_id:
            raise RuntimeError(
                "Airtable mirror requires AIRTABLE_API_KEY and AIRTABLE_BASE_ID. "
                "No live API call was made."
            )
        raise NotImplementedError("Airtable table mirroring is not implemented yet.")


@dataclass(frozen=True)
class GoogleSheetsMirrorProvider:
    """Placeholder Google Sheets adapter. No live API calls are implemented."""

    credentials_file: str | None = None
    spreadsheet_id: str | None = None
    worksheet_name: str | None = None
    live: bool = False
    name: TableMirrorProviderName = TableMirrorProviderName.GOOGLE_SHEETS

    def export_records(
        self,
        object_type: TableMirrorObjectType | str,
        records: Sequence[Any],
    ) -> TableMirrorExportResult:
        _ = records
        if not self.live:
            raise RuntimeError(
                "Google Sheets mirror requires explicit live=True. No live API call was made."
            )
        credentials_file = self.credentials_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        spreadsheet_id = self.spreadsheet_id or os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
        if not credentials_file or not spreadsheet_id:
            raise RuntimeError(
                "Google Sheets mirror requires GOOGLE_APPLICATION_CREDENTIALS and "
                "GOOGLE_SHEETS_SPREADSHEET_ID. No live API call was made."
            )
        raise NotImplementedError("Google Sheets table mirroring is not implemented yet.")


def build_table_mirror_provider(
    provider: TableMirrorProviderName | str,
    *,
    live: bool = False,
) -> TableMirrorProvider:
    """Build a provider-neutral table mirror adapter."""

    try:
        resolved_provider = normalize_table_mirror_provider(provider)
    except ValueError as exc:
        valid = ", ".join(item.value for item in TableMirrorProviderName)
        raise ValueError(
            f"Unsupported table mirror provider {provider!r}. Choose one of: {valid}."
        ) from exc
    if resolved_provider == TableMirrorProviderName.DRY_RUN:
        return DryRunTableMirrorProvider()
    if resolved_provider == TableMirrorProviderName.AIRTABLE:
        return AirtableMirrorProvider(live=live)
    if resolved_provider == TableMirrorProviderName.GOOGLE_SHEETS:
        return GoogleSheetsMirrorProvider(live=live)
    raise AssertionError(f"Unhandled table mirror provider: {resolved_provider}")


def build_local_crm_provider_from_table_records(
    records: Sequence[Any],
    *,
    account_contexts: Sequence[Any] = (),
) -> LocalTableMirrorCRMProvider:
    """Create a local-only CRM provider from fixture or table-mirror records."""

    leads = []
    for index, record in enumerate(records, start=1):
        data = _as_dict(record)
        leads.append(
            {
                "id": data.get("id") or data.get("record_id") or f"table-lead-{index}",
                "company_name": data.get("company_name") or data.get("name") or "",
                "contact_name": data.get("contact_name") or data.get("recipient") or "",
                "status": data.get("status") or data.get("approval_state") or "new",
                "source_id": data.get("source_id") or data.get("source") or f"table-lead-{index}",
                "source": data.get("source") or "fixture",
                "source_url": data.get("source_url") or data.get("source") or "",
            }
        )
    return LocalTableMirrorCRMProvider(
        leads=leads,
        account_contexts=account_contexts,
        dry_run=True,
    )


def build_crm_provider(
    provider: CRMProviderName | str = CRMProviderName.LOCAL_TABLE_MIRROR,
    *,
    leads: Sequence[Any] = (),
    account_contexts: Sequence[Any] = (),
    dry_run: bool = True,
    table_mirror_provider: TableMirrorProvider | None = None,
) -> LocalTableMirrorCRMProvider:
    """Build the local-only CRM provider.

    HubSpot, Airtable, Google Sheets, and other live CRM adapters are future work.
    """

    try:
        resolved_provider = normalize_crm_provider_name(provider)
    except ValueError as exc:
        raise ValueError(
            f"Unsupported CRM provider {provider!r}. Only "
            f"{CRMProviderName.LOCAL_TABLE_MIRROR.value!r} is available."
        ) from exc
    if resolved_provider != CRMProviderName.LOCAL_TABLE_MIRROR:
        raise AssertionError(f"Unhandled CRM provider: {resolved_provider}")
    if not dry_run:
        raise RuntimeError("Live CRM providers are not implemented; use dry_run=True.")
    return LocalTableMirrorCRMProvider(
        leads=leads,
        account_contexts=account_contexts,
        dry_run=dry_run,
        table_mirror_provider=table_mirror_provider or DryRunTableMirrorProvider(),
    )
