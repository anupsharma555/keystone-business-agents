"""Schemas for the local-only CRM provider abstraction."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    normalize_approval_scope,
    normalize_approval_state,
    state_allows_drafting,
)
from keystone_agents.schemas.company_profile import ClaimEvidenceRecord
from keystone_agents.schemas.contact_context import ContactRecord
from keystone_agents.schemas.table_mirror import TableMirrorExportResult


class CRMProviderName(StrEnum):
    """Supported CRM provider names.

    Version 1 intentionally supports only local table-mirror previews.
    """

    LOCAL_TABLE_MIRROR = "local-table-mirror"


class CRMWriteOperation(StrEnum):
    """Write-like CRM operations modeled for future provider adapters."""

    UPDATE_STATUS = "update_status"
    ATTACH_REPORT_LINK_OR_NOTE = "attach_report_link_or_note"


class CRMWriteStatus(StrEnum):
    """Outcome of a CRM write-like request."""

    BLOCKED_PENDING_APPROVAL = "blocked_pending_approval"
    DRY_RUN_READY = "dry_run_ready"


class CRMLeadRecord(BaseModel):
    """Provider-neutral lead/contact record for local CRM previews."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(min_length=1, validation_alias=AliasChoices("id", "lead_id"))
    company_name: str = Field(min_length=1)
    contact_name: str = ""
    role_title: str = Field(
        default="",
        validation_alias=AliasChoices("role_title", "contact_title", "title"),
    )
    contact_email: str | None = None
    linkedin_url: str | None = None
    status: str = "local_context"
    source: str = "fixture"
    source_id: str = "fixture:crm_lead"
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    notes: str = ""
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unsupported_claims_flagged: list[str] = Field(default_factory=list)

    @classmethod
    def from_contact(cls, contact: ContactRecord) -> CRMLeadRecord:
        """Build a CRM lead view from an approved or pending local contact."""

        return cls(
            id=contact.source_id,
            company_name=contact.company_name,
            contact_name=contact.contact_name,
            role_title=contact.role_title,
            contact_email=contact.contact_email,
            linkedin_url=contact.linkedin_url,
            status=contact.approval_state.value,
            source=contact.source,
            source_id=contact.source_id,
            source_url=contact.source_url,
            confidence=contact.confidence,
            approval_state=contact.approval_state,
            approval_scope=contact.approval_scope,
            notes=contact.notes,
            claims=contact.claims,
            unsupported_claims_flagged=contact.unsupported_claims_flagged,
        )

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @field_validator(
        "id",
        "company_name",
        "contact_name",
        "role_title",
        "status",
        "source",
        "source_id",
        "source_url",
        "notes",
        mode="before",
    )
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("contact_email", "linkedin_url", mode="before")
    @classmethod
    def _blank_optional_string_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @property
    def approved_for_crm_write(self) -> bool:
        return (
            state_allows_drafting(self.approval_state)
            and self.approval_scope == ApprovalScope.DRAFTING
        )


class CRMWriteResult(BaseModel):
    """Dry-run audit result for a CRM write-like operation."""

    model_config = ConfigDict(extra="forbid")

    provider: CRMProviderName = CRMProviderName.LOCAL_TABLE_MIRROR
    operation: CRMWriteOperation
    object_id: str = Field(min_length=1)
    company_name: str = ""
    dry_run: bool = True
    approval_required: bool = True
    approval_state: ApprovalState = ApprovalState.PENDING
    reviewer: str = ""
    status: CRMWriteStatus
    requested_status: str | None = None
    report_link: str | None = None
    note: str = ""
    table_preview: TableMirrorExportResult | None = None
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("object_id", "company_name", "reviewer", "note", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("requested_status", "report_link", mode="before")
    @classmethod
    def _blank_optional_string_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("audit_notes", mode="before")
    @classmethod
    def _normalize_audit_notes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def normalize_crm_provider_name(value: CRMProviderName | str) -> CRMProviderName:
    """Normalize CRM provider CLI/config values."""

    if isinstance(value, CRMProviderName):
        return value
    normalized = str(value).strip().lower().replace("_", "-")
    try:
        return CRMProviderName(normalized)
    except ValueError as exc:
        valid = ", ".join(item.value for item in CRMProviderName)
        raise ValueError(f"CRM provider must be one of: {valid}") from exc
