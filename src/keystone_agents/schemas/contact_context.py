"""Local contact and CRM account context schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    normalize_approval_scope,
    normalize_approval_state,
    state_allows_drafting,
)
from keystone_agents.schemas.company_profile import ClaimEvidenceRecord, SourceRecord


def _source_url(source: str, source_url: str | None, fallback: str) -> str:
    resolved = str(source_url or "").strip()
    if resolved:
        return resolved
    if source.startswith("fixture://") or source.startswith("local://"):
        return source
    return fallback


def _source_type(source: str, source_url: str) -> Literal["fixture", "user_provided"]:
    source_text = f"{source} {source_url}".lower()
    if "fixture" in source_text:
        return "fixture"
    return "user_provided"


def _source_backed_claims(
    claims: list[ClaimEvidenceRecord],
    source_id: str,
) -> list[ClaimEvidenceRecord]:
    return [
        claim
        for claim in claims
        if claim.source_id == source_id
        and claim.confidence > 0.0
        and claim.approved
        and claim.claim_type != "unsupported"
    ]


class ContactRecord(BaseModel):
    """Approved or pending local contact context for drafting personalization."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    company_name: str = Field(
        min_length=1,
        validation_alias=AliasChoices("company_name", "company"),
    )
    contact_name: str = Field(
        min_length=1,
        validation_alias=AliasChoices("contact_name", "name"),
    )
    role_title: str = Field(
        default="",
        validation_alias=AliasChoices("role_title", "contact_title", "title"),
    )
    contact_email: str | None = Field(
        default=None,
        validation_alias=AliasChoices("contact_email", "email", "recipient_email"),
    )
    linkedin_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("linkedin_url", "profile_url"),
    )
    source: str = "fixture"
    source_id: str = "fixture:contact"
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_state: ApprovalState = Field(
        default=ApprovalState.PENDING,
        validation_alias=AliasChoices("approval_state", "approval_status"),
    )
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    notes: str = ""
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unsupported_claims_flagged: list[str] = Field(default_factory=list)

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("contact_email", "linkedin_url", mode="before")
    @classmethod
    def _blank_optional_string_to_none(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @model_validator(mode="after")
    def _populate_source_backed_claims(self) -> ContactRecord:
        self.source = str(self.source or "fixture").strip() or "fixture"
        self.source_id = str(self.source_id or "fixture:contact").strip() or "fixture:contact"
        self.source_url = _source_url(
            self.source,
            self.source_url,
            f"fixture://{self.source_id.replace(':', '_')}",
        )
        if not self.claims:
            title_fragment = f" as {self.role_title}" if self.role_title else ""
            self.claims = [
                ClaimEvidenceRecord(
                    claim_text=(
                        f"Contact context identifies {self.contact_name}{title_fragment} "
                        f"at {self.company_name}."
                    ),
                    source_id=self.source_id,
                    confidence=self.confidence or 0.65,
                    claim_type="contact_context",
                )
            ]
        unsupported = [
            f"unbacked contact context claim: {claim.claim_text}"
            for claim in self.claims
            if claim.source_id != self.source_id
            or claim.confidence <= 0.0
            or claim.claim_type == "unsupported"
        ]
        self.unsupported_claims_flagged = list(
            dict.fromkeys([*self.unsupported_claims_flagged, *unsupported])
        )
        return self

    @property
    def approval_status(self) -> str:
        return self.approval_state.value

    @property
    def name(self) -> str:
        return self.contact_name

    @property
    def title(self) -> str:
        return self.role_title

    @property
    def contact_title(self) -> str:
        return self.role_title

    @property
    def email(self) -> str | None:
        return self.contact_email

    @property
    def profile_url(self) -> str | None:
        return self.linkedin_url

    @property
    def approved_for_personalization(self) -> bool:
        return (
            state_allows_drafting(self.approval_state)
            and self.approval_scope == ApprovalScope.DRAFTING
        )

    def source_backed_claims(self) -> list[ClaimEvidenceRecord]:
        return _source_backed_claims(self.claims, self.source_id)

    def to_source_record(self) -> SourceRecord:
        supported_claims = [claim.claim_text for claim in self.source_backed_claims()]
        if not supported_claims:
            supported_claims = [
                f"Local contact context exists for {self.contact_name} at {self.company_name}."
            ]
        return SourceRecord(
            source_id=self.source_id,
            title=f"Local contact context for {self.company_name}",
            url=self.source_url,
            source_type=_source_type(self.source, self.source_url),
            supported_claims=supported_claims,
            confidence=self.confidence or 0.65,
        )


class CRMAccountContext(BaseModel):
    """Approved or pending local CRM/account context for research and drafting."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    company_name: str = Field(min_length=1)
    account_stage: str = ""
    account_owner: str = ""
    last_touchpoint: str = Field(
        default="",
        validation_alias=AliasChoices("last_touchpoint", "last_touch", "recent_touchpoint"),
    )
    next_step: str = ""
    source: str = "fixture"
    source_id: str = "fixture:crm_context"
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    notes: str = ""
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unsupported_claims_flagged: list[str] = Field(default_factory=list)

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @model_validator(mode="after")
    def _populate_source_backed_claims(self) -> CRMAccountContext:
        self.source = str(self.source or "fixture").strip() or "fixture"
        self.source_id = (
            str(self.source_id or "fixture:crm_context").strip() or "fixture:crm_context"
        )
        self.source_url = _source_url(
            self.source,
            self.source_url,
            f"fixture://{self.source_id.replace(':', '_')}",
        )
        if not self.claims:
            claim_parts = [
                f"CRM account context for {self.company_name}",
                f"stage: {self.account_stage}" if self.account_stage else "",
                f"owner: {self.account_owner}" if self.account_owner else "",
                f"last touchpoint: {self.last_touchpoint}" if self.last_touchpoint else "",
                f"next step: {self.next_step}" if self.next_step else "",
                self.notes,
            ]
            claim_text = "; ".join(part for part in claim_parts if part).strip()
            self.claims = [
                ClaimEvidenceRecord(
                    claim_text=claim_text,
                    source_id=self.source_id,
                    confidence=self.confidence or 0.65,
                    claim_type="user_provided",
                )
            ]
        unsupported = [
            f"unbacked CRM account context claim: {claim.claim_text}"
            for claim in self.claims
            if claim.source_id != self.source_id
            or claim.confidence <= 0.0
            or claim.claim_type == "unsupported"
        ]
        self.unsupported_claims_flagged = list(
            dict.fromkeys([*self.unsupported_claims_flagged, *unsupported])
        )
        return self

    @property
    def approval_status(self) -> str:
        return self.approval_state.value

    @property
    def approved_for_personalization(self) -> bool:
        return (
            state_allows_drafting(self.approval_state)
            and self.approval_scope == ApprovalScope.DRAFTING
        )

    def source_backed_claims(self) -> list[ClaimEvidenceRecord]:
        return _source_backed_claims(self.claims, self.source_id)

    def to_source_record(self) -> SourceRecord:
        supported_claims = [claim.claim_text for claim in self.source_backed_claims()]
        if not supported_claims:
            supported_claims = [f"Local CRM account context exists for {self.company_name}."]
        return SourceRecord(
            source_id=self.source_id,
            title=f"Local CRM account context for {self.company_name}",
            url=self.source_url,
            source_type=_source_type(self.source, self.source_url),
            supported_claims=supported_claims,
            confidence=self.confidence or 0.65,
        )


class LocalAccountContext(BaseModel):
    """Bundle of local contact and CRM context for one account."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    contacts: list[ContactRecord] = Field(default_factory=list)
    crm_context: CRMAccountContext | None = None
