"""Schemas for provider-neutral external table mirroring."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TableMirrorProviderName(StrEnum):
    DRY_RUN = "dry-run"
    AIRTABLE = "airtable"
    GOOGLE_SHEETS = "google-sheets"


class TableMirrorObjectType(StrEnum):
    OPPORTUNITIES = "opportunities"
    COMPANIES = "companies"
    DRAFTS = "drafts"
    APPROVALS = "approvals"
    FEEDBACK = "feedback"
    AUDIT_RECORDS = "audit_records"
    CONTACTS = "contacts"
    CRM_CONTEXTS = "crm_contexts"
    OUTREACH_TRACKING = "outreach_tracking"


TableCellValue = str | int | float | bool | None


class TableColumnMapping(BaseModel):
    """One source field to external table column mapping."""

    model_config = ConfigDict(extra="forbid")

    source_field: str = Field(min_length=1)
    column_name: str = Field(min_length=1)
    description: str = ""


class TableMirrorRecord(BaseModel):
    """One normalized row ready to mirror into an external table."""

    model_config = ConfigDict(extra="forbid")

    object_type: TableMirrorObjectType
    record_id: str = Field(min_length=1)
    fields: dict[str, TableCellValue] = Field(default_factory=dict)
    source_model: str = ""
    source_ref: str = ""

    @field_validator("fields")
    @classmethod
    def reject_empty_column_names(
        cls,
        fields: dict[str, TableCellValue],
    ) -> dict[str, TableCellValue]:
        for column in fields:
            if not column.strip():
                raise ValueError("table mirror column names must be non-empty")
        return fields


class TableMirrorExportResult(BaseModel):
    """Dry-run or placeholder live-provider mirror result."""

    model_config = ConfigDict(extra="forbid")

    provider: TableMirrorProviderName
    object_type: TableMirrorObjectType
    table_name: str = Field(min_length=1)
    dry_run: bool = True
    live_provider_requested: bool = False
    columns: list[TableColumnMapping] = Field(default_factory=list)
    records: list[TableMirrorRecord] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)

    @property
    def field_to_column(self) -> dict[str, str]:
        return {column.source_field: column.column_name for column in self.columns}


class TableMirrorExportRequest(BaseModel):
    """Provider-neutral request metadata for a mirror export."""

    model_config = ConfigDict(extra="forbid")

    provider: TableMirrorProviderName = TableMirrorProviderName.DRY_RUN
    object_type: TableMirrorObjectType
    dry_run: bool = True
    live_provider_requested: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


TABLE_NAME_BY_OBJECT_TYPE: dict[TableMirrorObjectType, str] = {
    TableMirrorObjectType.OPPORTUNITIES: "Opportunities",
    TableMirrorObjectType.COMPANIES: "Company Profiles",
    TableMirrorObjectType.DRAFTS: "Outreach Drafts",
    TableMirrorObjectType.APPROVALS: "Approval Queue",
    TableMirrorObjectType.FEEDBACK: "Feedback",
    TableMirrorObjectType.AUDIT_RECORDS: "Audit Records",
    TableMirrorObjectType.CONTACTS: "Contacts",
    TableMirrorObjectType.CRM_CONTEXTS: "CRM Account Context",
    TableMirrorObjectType.OUTREACH_TRACKING: "Outreach Tracking",
}

TABLE_COLUMN_MAPPINGS: dict[TableMirrorObjectType, tuple[TableColumnMapping, ...]] = {
    TableMirrorObjectType.OPPORTUNITIES: (
        TableColumnMapping(source_field="company_name", column_name="Company Name"),
        TableColumnMapping(source_field="opportunity_type", column_name="Opportunity Type"),
        TableColumnMapping(source_field="priority_score", column_name="Priority Score"),
        TableColumnMapping(source_field="why_now_signal", column_name="Why Now Signal"),
        TableColumnMapping(
            source_field="recommended_next_step",
            column_name="Recommended Next Step",
        ),
        TableColumnMapping(source_field="keystone_fit_reason", column_name="Keystone Fit Reason"),
        TableColumnMapping(
            source_field="outside_consulting_likelihood",
            column_name="Outside Consulting Likelihood",
        ),
        TableColumnMapping(
            source_field="approval_required_before_outreach",
            column_name="Approval Required Before Outreach",
        ),
        TableColumnMapping(source_field="source_signals", column_name="Source Signals"),
        TableColumnMapping(source_field="sources.url", column_name="Source URLs"),
    ),
    TableMirrorObjectType.COMPANIES: (
        TableColumnMapping(source_field="name", column_name="Company Name"),
        TableColumnMapping(source_field="website", column_name="Website"),
        TableColumnMapping(source_field="lead_name", column_name="Lead Name"),
        TableColumnMapping(source_field="linkedin_url", column_name="LinkedIn URL"),
        TableColumnMapping(source_field="description", column_name="Description"),
        TableColumnMapping(source_field="fit_summary", column_name="Fit Summary"),
        TableColumnMapping(source_field="consulting_fit_score", column_name="Consulting Fit Score"),
        TableColumnMapping(source_field="confidence_score", column_name="Confidence Score"),
        TableColumnMapping(source_field="evidence", column_name="Evidence"),
        TableColumnMapping(source_field="risks", column_name="Risks"),
        TableColumnMapping(source_field="missing_information", column_name="Missing Information"),
        TableColumnMapping(source_field="sources.url", column_name="Source URLs"),
    ),
    TableMirrorObjectType.DRAFTS: (
        TableColumnMapping(source_field="company_name", column_name="Company Name"),
        TableColumnMapping(source_field="contact_name", column_name="Contact Name"),
        TableColumnMapping(source_field="contact_title", column_name="Contact Title"),
        TableColumnMapping(source_field="outreach_goal", column_name="Outreach Goal"),
        TableColumnMapping(source_field="email_subject", column_name="Email Subject"),
        TableColumnMapping(source_field="email_body", column_name="Email Body Summary"),
        TableColumnMapping(source_field="linkedin_note", column_name="LinkedIn Note"),
        TableColumnMapping(source_field="approval_state", column_name="Approval State"),
        TableColumnMapping(source_field="approval_scope", column_name="Approval Scope"),
        TableColumnMapping(source_field="template_id", column_name="Template ID"),
        TableColumnMapping(source_field="example_ids_used", column_name="Example IDs Used"),
        TableColumnMapping(source_field="lifecycle_status", column_name="Lifecycle Status"),
        TableColumnMapping(source_field="outreach_sent", column_name="Outreach Sent"),
        TableColumnMapping(source_field="sent_at", column_name="Sent At"),
        TableColumnMapping(source_field="reply_received", column_name="Reply Received"),
        TableColumnMapping(source_field="reply_received_at", column_name="Reply Received At"),
        TableColumnMapping(source_field="outcome", column_name="Outcome"),
        TableColumnMapping(source_field="outcome_notes", column_name="Outcome Notes"),
        TableColumnMapping(source_field="next_step", column_name="Next Step"),
        TableColumnMapping(
            source_field="unsupported_claims_flagged",
            column_name="Unsupported Claims",
        ),
    ),
    TableMirrorObjectType.APPROVALS: (
        TableColumnMapping(source_field="object_type", column_name="Object Type"),
        TableColumnMapping(source_field="object_id", column_name="Object ID"),
        TableColumnMapping(source_field="summary", column_name="Summary"),
        TableColumnMapping(source_field="decision", column_name="Decision"),
        TableColumnMapping(source_field="scope", column_name="Scope"),
        TableColumnMapping(source_field="reviewer", column_name="Reviewer"),
        TableColumnMapping(source_field="timestamp", column_name="Timestamp"),
        TableColumnMapping(source_field="notes", column_name="Notes"),
        TableColumnMapping(source_field="risk_flags", column_name="Risk Flags"),
        TableColumnMapping(source_field="slack_ts", column_name="Slack Thread TS"),
    ),
    TableMirrorObjectType.FEEDBACK: (
        TableColumnMapping(source_field="id", column_name="ID"),
        TableColumnMapping(source_field="object_type", column_name="Object Type"),
        TableColumnMapping(source_field="object_id", column_name="Object ID"),
        TableColumnMapping(source_field="rating", column_name="Rating"),
        TableColumnMapping(source_field="tags", column_name="Tags"),
        TableColumnMapping(source_field="notes", column_name="Notes"),
        TableColumnMapping(source_field="created_at", column_name="Created At"),
    ),
    TableMirrorObjectType.AUDIT_RECORDS: (
        TableColumnMapping(source_field="id", column_name="ID"),
        TableColumnMapping(source_field="agent_name", column_name="Agent"),
        TableColumnMapping(source_field="status", column_name="Status"),
        TableColumnMapping(source_field="dry_run", column_name="Dry Run"),
        TableColumnMapping(source_field="input_summary", column_name="Input Summary"),
        TableColumnMapping(source_field="created_at_utc", column_name="Created At UTC"),
    ),
    TableMirrorObjectType.CONTACTS: (
        TableColumnMapping(source_field="contact_name", column_name="Contact Name"),
        TableColumnMapping(source_field="contact_email", column_name="Contact Email"),
        TableColumnMapping(source_field="role_title", column_name="Role/Title"),
        TableColumnMapping(source_field="company_name", column_name="Company Name"),
        TableColumnMapping(source_field="linkedin_url", column_name="LinkedIn URL"),
        TableColumnMapping(source_field="source", column_name="Source"),
        TableColumnMapping(source_field="source_id", column_name="Source ID"),
        TableColumnMapping(source_field="source_url", column_name="Source URL"),
        TableColumnMapping(source_field="confidence", column_name="Confidence"),
        TableColumnMapping(source_field="status", column_name="CRM Status"),
        TableColumnMapping(source_field="approval_state", column_name="Approval State"),
        TableColumnMapping(source_field="approval_scope", column_name="Approval Scope"),
        TableColumnMapping(source_field="notes", column_name="Notes"),
    ),
    TableMirrorObjectType.CRM_CONTEXTS: (
        TableColumnMapping(source_field="company_name", column_name="Company Name"),
        TableColumnMapping(source_field="account_stage", column_name="Account Stage"),
        TableColumnMapping(source_field="account_owner", column_name="Account Owner"),
        TableColumnMapping(source_field="last_touchpoint", column_name="Last Touchpoint"),
        TableColumnMapping(source_field="next_step", column_name="Next Step"),
        TableColumnMapping(source_field="source", column_name="Source"),
        TableColumnMapping(source_field="source_id", column_name="Source ID"),
        TableColumnMapping(source_field="source_url", column_name="Source URL"),
        TableColumnMapping(source_field="confidence", column_name="Confidence"),
        TableColumnMapping(source_field="approval_state", column_name="Approval State"),
        TableColumnMapping(source_field="approval_scope", column_name="Approval Scope"),
        TableColumnMapping(source_field="notes", column_name="Notes"),
    ),
    TableMirrorObjectType.OUTREACH_TRACKING: (
        TableColumnMapping(source_field="draft_id", column_name="Draft ID"),
        TableColumnMapping(source_field="company_name", column_name="Company Name"),
        TableColumnMapping(source_field="contact_name", column_name="Contact Name"),
        TableColumnMapping(source_field="channel", column_name="Channel"),
        TableColumnMapping(source_field="lifecycle_status", column_name="Lifecycle Status"),
        TableColumnMapping(source_field="outreach_sent", column_name="Outreach Sent"),
        TableColumnMapping(source_field="sent_at", column_name="Sent At"),
        TableColumnMapping(source_field="sent_by", column_name="Sent By"),
        TableColumnMapping(source_field="sent_via", column_name="Sent Via"),
        TableColumnMapping(source_field="reply_received", column_name="Reply Received"),
        TableColumnMapping(source_field="reply_received_at", column_name="Reply Received At"),
        TableColumnMapping(source_field="reply_summary", column_name="Reply Summary"),
        TableColumnMapping(source_field="outcome", column_name="Outcome"),
        TableColumnMapping(source_field="outcome_notes", column_name="Outcome Notes"),
        TableColumnMapping(source_field="next_step", column_name="Next Step"),
        TableColumnMapping(source_field="last_checked_at", column_name="Last Checked At"),
        TableColumnMapping(source_field="manual_update_only", column_name="Manual Update Only"),
        TableColumnMapping(source_field="send_enabled", column_name="Send Enabled"),
        TableColumnMapping(source_field="sent_by_agent", column_name="Sent By Agent"),
    ),
}


def normalize_table_mirror_object_type(value: TableMirrorObjectType | str) -> TableMirrorObjectType:
    """Normalize object type CLI strings."""

    if isinstance(value, TableMirrorObjectType):
        return value
    try:
        return TableMirrorObjectType(str(value).strip().lower())
    except ValueError as exc:
        valid = ", ".join(item.value for item in TableMirrorObjectType)
        raise ValueError(f"table mirror object type must be one of: {valid}") from exc


def normalize_table_mirror_provider(
    value: TableMirrorProviderName | str,
) -> TableMirrorProviderName:
    """Normalize provider CLI strings."""

    if isinstance(value, TableMirrorProviderName):
        return value
    normalized = str(value).strip().lower().replace("_", "-")
    try:
        return TableMirrorProviderName(normalized)
    except ValueError as exc:
        valid = ", ".join(item.value for item in TableMirrorProviderName)
        raise ValueError(f"table mirror provider must be one of: {valid}") from exc


def table_name_for_object_type(object_type: TableMirrorObjectType | str) -> str:
    """Return the external table name for an object type."""

    return TABLE_NAME_BY_OBJECT_TYPE[normalize_table_mirror_object_type(object_type)]


def column_mappings_for_object_type(
    object_type: TableMirrorObjectType | str,
) -> list[TableColumnMapping]:
    """Return source-field to column mappings for an object type."""

    return [
        mapping.model_copy()
        for mapping in TABLE_COLUMN_MAPPINGS[normalize_table_mirror_object_type(object_type)]
    ]
