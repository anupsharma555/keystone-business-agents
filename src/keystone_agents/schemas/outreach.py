"""Outreach draft schemas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    external_use_state_from_legacy,
    normalize_approval_scope,
    state_allows_external_use,
)
from keystone_agents.schemas.company_profile import ClaimEvidenceRecord, CompanyProfile
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.email_style import EmailStyleProfile

EM_DASH = "\u2014"
PROFESSIONAL_ADVICE_TERMS = (
    "medical advice",
    "legal advice",
    "tax advice",
    "regulatory advice",
    "diagnose",
    "prescribe",
)
FollowUpStatus = Literal[
    "recommended",
    "pending_approval",
    "approved",
    "rejected",
    "completed",
    "cancelled",
]
OutreachChannel = Literal["email", "linkedin", "other"]
OutreachLifecycleStatus = Literal[
    "not_started",
    "draft_pending_approval",
    "draft_approved",
    "draft_created",
    "sent_manually",
    "reply_received",
    "closed_no_reply",
    "bounced",
    "paused",
]
OutreachOutcome = Literal[
    "unknown",
    "pending_reply",
    "positive_reply",
    "neutral_reply",
    "negative_reply",
    "meeting_booked",
    "not_interested",
    "bounced",
    "converted",
    "closed_no_reply",
    "do_not_contact",
]
OutreachDraftStatus = Literal["blocked", "clarification_required"]


_SENT_LIFECYCLE_STATUSES = {
    "sent_manually",
    "reply_received",
    "closed_no_reply",
    "bounced",
}
_SENT_OUTCOMES = {
    "pending_reply",
    "positive_reply",
    "neutral_reply",
    "negative_reply",
    "meeting_booked",
    "not_interested",
    "bounced",
    "converted",
    "closed_no_reply",
    "do_not_contact",
}
_REPLY_OUTCOMES = {
    "positive_reply",
    "neutral_reply",
    "negative_reply",
    "meeting_booked",
    "not_interested",
    "converted",
    "do_not_contact",
}


def _created_at() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_follow_up_date(days: int = 7) -> str:
    """Return a YYYY-MM-DD proposed follow-up date."""

    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def _validate_internal_copy(value: str) -> str:
    if EM_DASH in value:
        raise ValueError("draft artifacts must not contain em dashes")
    lowered = value.lower()
    if any(term in lowered for term in PROFESSIONAL_ADVICE_TERMS):
        raise ValueError("draft artifacts must not provide professional advice")
    return value


class OpportunityRecord(BaseModel):
    """Approved opportunity context used by the outreach composer."""

    company_name: str
    title: str = ""
    source: str = "fixture"
    source_id: str = "fixture:opportunity"
    notes: str = ""
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    rationale: str = ""
    next_step: str = ""
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unsupported_claims_flagged: list[str] = Field(default_factory=list)
    approved_for_outreach: bool = True

    @model_validator(mode="after")
    def populate_claims(self) -> OpportunityRecord:
        if not self.claims:
            claim_text = (self.rationale or self.notes or self.title).strip()
            if claim_text:
                self.claims = [
                    ClaimEvidenceRecord(
                        claim_text=claim_text,
                        source_id=self.source_id,
                        confidence=0.7,
                        claim_type="opportunity_signal",
                    )
                ]
        return self


class OutreachTemplateContext(BaseModel):
    """Approved outreach template guidance. Templates provide structure, not facts."""

    template_id: str = Field(min_length=1)
    template_version: str = "v1"
    name: str = ""
    stage: str = ""
    tone_guidance: list[str] = Field(default_factory=list)
    structure_guidance: list[str] = Field(default_factory=list)
    pacing_guidance: str = ""
    cta_guidance: str = ""
    follow_up_pattern: str = ""
    fit_reason: str = ""
    approved: bool = True
    provides_factual_claims: bool = False
    send_enabled: bool = False

    @field_validator(
        "template_id",
        "template_version",
        "name",
        "stage",
        "pacing_guidance",
        "cta_guidance",
        "follow_up_pattern",
        "fit_reason",
    )
    @classmethod
    def _safe_text_fields(cls, value: str) -> str:
        return _validate_internal_copy(str(value).strip())

    @field_validator("tone_guidance", "structure_guidance")
    @classmethod
    def _safe_guidance_lists(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("approved")
    @classmethod
    def _must_be_approved(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach templates must be approved before drafting")
        return value

    @field_validator("provides_factual_claims", "send_enabled")
    @classmethod
    def _advisory_only_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach templates guide structure only and cannot enable send")
        return value


class OutreachExampleGuidance(BaseModel):
    """Approved RAG example guidance. Examples provide style guidance, not facts."""

    example_id: str = Field(min_length=1)
    title: str = ""
    source_id: str = ""
    matched_query: str = ""
    tone_guidance: list[str] = Field(default_factory=list)
    structure_guidance: list[str] = Field(default_factory=list)
    pacing_guidance: str = ""
    cta_guidance: str = ""
    follow_up_pattern: str = ""
    applicability_notes: str = ""
    approved: bool = True
    raw_email_body_included: bool = False
    provides_factual_claims: bool = False
    send_enabled: bool = False

    @field_validator(
        "example_id",
        "title",
        "source_id",
        "matched_query",
        "pacing_guidance",
        "cta_guidance",
        "follow_up_pattern",
        "applicability_notes",
    )
    @classmethod
    def _safe_text_fields(cls, value: str) -> str:
        return _validate_internal_copy(str(value).strip())

    @field_validator("tone_guidance", "structure_guidance")
    @classmethod
    def _safe_guidance_lists(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("approved")
    @classmethod
    def _must_be_approved(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("RAG example guidance must be approved before drafting")
        return value

    @field_validator("raw_email_body_included", "provides_factual_claims", "send_enabled")
    @classmethod
    def _advisory_only_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError(
                "RAG examples guide tone and structure only; they cannot include raw "
                "email bodies, factual claims, or send enablement"
            )
        return value


class OutreachContext(BaseModel):
    """Approved context envelope used to compose draft-only outreach."""

    company_profile: CompanyProfile | None = None
    opportunity_record: OpportunityRecord | None = None
    contact_context: ContactRecord | None = None
    crm_context: CRMAccountContext | None = None
    email_style_profile: EmailStyleProfile | None = None
    allowed_keystone_positioning: list[ClaimEvidenceRecord] = Field(default_factory=list)
    facts_used: list[ClaimEvidenceRecord] = Field(default_factory=list)
    blocked_facts: list[str] = Field(default_factory=list)
    approval_state: ApprovalState = ApprovalState.PENDING
    approved_context_used: bool = False
    outreach_template: OutreachTemplateContext | None = None
    example_guidance: list[OutreachExampleGuidance] = Field(default_factory=list)

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_context_approval_state(cls, value: Any) -> ApprovalState:
        return external_use_state_from_legacy(value)

    @field_validator("blocked_facts")
    @classmethod
    def _safe_blocked_facts(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]


class ApprovedOutreachDraftingContext(BaseModel):
    """Typed context envelope for constrained LLM outreach drafting."""

    company_profile: CompanyProfile
    opportunity_record: OpportunityRecord | None = None
    contact_context: ContactRecord | None = None
    crm_context: CRMAccountContext | None = None
    email_style_profile: EmailStyleProfile | None = None
    allowed_facts: list[ClaimEvidenceRecord] = Field(min_length=1)
    blocked_facts: list[str] = Field(default_factory=list)
    objective: str
    revision_request: str = ""
    max_variants: int = Field(default=1, ge=1, le=3)
    outreach_template: OutreachTemplateContext | None = None
    example_guidance: list[OutreachExampleGuidance] = Field(default_factory=list)
    approval_state: ApprovalState = ApprovalState.PENDING
    approved_context_used: bool = True
    allowed_source_ids: list[str] = Field(default_factory=list)

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_context_approval_state(cls, value: Any) -> ApprovalState:
        return external_use_state_from_legacy(value)

    @field_validator("blocked_facts")
    @classmethod
    def _safe_blocked_facts(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("objective", "revision_request")
    @classmethod
    def _safe_instruction_text(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("allowed_facts")
    @classmethod
    def _allowed_facts_must_be_source_backed(
        cls,
        values: list[ClaimEvidenceRecord],
    ) -> list[ClaimEvidenceRecord]:
        blocked = [
            fact.claim_text
            for fact in values
            if fact.claim_type == "unsupported"
            or not fact.approved
            or fact.confidence <= 0
            or fact.source_id.startswith("unbacked:")
        ]
        if blocked:
            raise ValueError(
                "LLM drafting context allowed_facts must be approved source-backed claims"
            )
        return values

    @model_validator(mode="after")
    def _populate_allowed_source_ids(self) -> ApprovedOutreachDraftingContext:
        if self.approval_state != ApprovalState.PENDING:
            raise ValueError("LLM drafting context must remain pending external-use approval")
        if not self.approved_context_used:
            raise ValueError("LLM drafting requires approved context")
        if not self.allowed_source_ids:
            self.allowed_source_ids = list(
                dict.fromkeys(fact.source_id for fact in self.allowed_facts if fact.source_id)
            )
        return self


class CallPrepArtifact(BaseModel):
    """Draft-only internal call-prep artifact, not outbound communication."""

    discovery_questions: list[str] = Field(default_factory=list)
    meeting_objectives: list[str] = Field(default_factory=list)
    known_facts: list[ClaimEvidenceRecord] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    suggested_next_step: str = ""
    source_ids_used: list[str] = Field(default_factory=list)
    draft_only_internal: bool = True
    approval_required: bool = True

    @field_validator("discovery_questions", "meeting_objectives", "unknowns", "risks")
    @classmethod
    def _safe_text_list(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("suggested_next_step")
    @classmethod
    def _safe_next_step(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("known_facts")
    @classmethod
    def _known_facts_must_be_source_backed(
        cls,
        values: list[ClaimEvidenceRecord],
    ) -> list[ClaimEvidenceRecord]:
        blocked = [
            fact.claim_text
            for fact in values
            if fact.claim_type == "unsupported"
            or not fact.approved
            or fact.confidence <= 0
            or fact.source_id.startswith(("unbacked:", "user:"))
        ]
        if blocked:
            raise ValueError("call prep known facts must be approved source-backed claims")
        return values

    @field_validator("draft_only_internal", "approval_required")
    @classmethod
    def _must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("call prep is draft-only internal material requiring review")
        return value

    @model_validator(mode="after")
    def _populate_source_ids(self) -> CallPrepArtifact:
        if not self.source_ids_used and self.known_facts:
            self.source_ids_used = list(dict.fromkeys(fact.source_id for fact in self.known_facts))
        return self


class FollowUpScheduleRecord(BaseModel):
    """Data-only follow-up recommendation requiring human approval."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    company_name: str = Field(
        min_length=1,
        validation_alias=AliasChoices("company_name", "company"),
    )
    contact_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("contact_name", "contact"),
    )
    related_draft_id: str = ""
    proposed_date: str = Field(default_factory=default_follow_up_date)
    sequence_number: int = Field(default=1, ge=1)
    status: FollowUpStatus = "recommended"
    rationale: str
    approval_required: bool = True
    created_at: str = Field(default_factory=_created_at)
    send_enabled: bool = False
    sent: bool = False
    gmail_scheduled: bool = False
    background_job_created: bool = False

    @field_validator("rationale")
    @classmethod
    def _safe_rationale(cls, value: str) -> str:
        cleaned = _validate_internal_copy(value.strip())
        if not cleaned:
            raise ValueError("follow-up schedule rationale is required")
        return cleaned

    @field_validator("approval_required")
    @classmethod
    def _approval_required_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("follow-up schedules require human approval")
        return value

    @field_validator("send_enabled", "sent", "gmail_scheduled", "background_job_created")
    @classmethod
    def _side_effect_fields_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("follow-up schedules are data-only recommendations")
        return value

    @property
    def company(self) -> str:
        return self.company_name

    @property
    def contact(self) -> str | None:
        return self.contact_name


class OutreachTrackingRecord(BaseModel):
    """Manual lifecycle snapshot for a draft or outreach thread."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    draft_id: str = Field(min_length=1, validation_alias=AliasChoices("draft_id", "object_id"))
    company_name: str = ""
    contact_name: str = ""
    channel: OutreachChannel = "email"
    lifecycle_status: OutreachLifecycleStatus = "not_started"
    outreach_sent: bool = False
    sent_at: str = ""
    sent_by: str = ""
    sent_via: str = ""
    reply_received: bool = False
    reply_received_at: str = ""
    reply_summary: str = ""
    outcome: OutreachOutcome = "unknown"
    outcome_notes: str = ""
    next_step: str = ""
    last_checked_at: str = ""
    created_at: str = Field(default_factory=_created_at)
    manual_update_only: bool = True
    send_enabled: bool = False
    sent_by_agent: bool = False

    @field_validator(
        "draft_id",
        "company_name",
        "contact_name",
        "sent_at",
        "sent_by",
        "sent_via",
        "reply_received_at",
        "reply_summary",
        "outcome_notes",
        "next_step",
        "last_checked_at",
    )
    @classmethod
    def _safe_text_fields(cls, value: str) -> str:
        return _validate_internal_copy(str(value).strip())

    @field_validator("manual_update_only")
    @classmethod
    def _manual_update_only_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach tracking is a manual lifecycle record")
        return value

    @field_validator("send_enabled", "sent_by_agent")
    @classmethod
    def _automation_fields_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach tracking must not enable sending or agent-sent status")
        return value

    @model_validator(mode="after")
    def _normalize_lifecycle_booleans(self) -> OutreachTrackingRecord:
        if self.outcome in _REPLY_OUTCOMES:
            self.reply_received = True
        if self.lifecycle_status == "reply_received":
            self.reply_received = True
        if self.reply_received:
            self.outreach_sent = True
            self.lifecycle_status = "reply_received"
        if self.lifecycle_status in _SENT_LIFECYCLE_STATUSES:
            self.outreach_sent = True
        if self.outcome in _SENT_OUTCOMES:
            self.outreach_sent = True
        if self.lifecycle_status == "bounced":
            self.outcome = "bounced"
        elif self.lifecycle_status == "closed_no_reply":
            self.outcome = "closed_no_reply"
        elif self.outreach_sent and self.outcome == "unknown":
            self.outcome = "pending_reply"
        return self


class OutreachDraft(BaseModel):
    """Draft-only outbound copy pending human approval."""

    model_config = ConfigDict(populate_by_name=True)

    company_name: str = ""
    recipient: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None
    outreach_goal: str = ""
    email_subject: str = ""
    email_body: str = ""
    linkedin_note: str = ""
    personalization_rationale: str = ""
    facts_used: list[ClaimEvidenceRecord] = Field(default_factory=list)
    blocked_facts: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)
    outreach_context: OutreachContext | None = None
    style_profile_used: bool = False
    style_profile_id: str = ""
    template_id: str = ""
    template_version: str = ""
    template_fit_reason: str = ""
    example_ids_used: list[str] = Field(default_factory=list)
    example_guidance_used: bool = False
    call_prep: CallPrepArtifact | None = None
    follow_up_schedules: list[FollowUpScheduleRecord] = Field(default_factory=list)
    drafting_mode: Literal["deterministic_fixture", "llm_constrained"] = "deterministic_fixture"
    revision_request: str = ""
    draft_policy: Literal["normal", "acknowledgement_only", "refused"] = "normal"
    approved_context_used: bool = False
    unsupported_claims_flagged: list[str] = Field(default_factory=list)
    unsupported_claim_explanations: list[str] = Field(default_factory=list)
    approval_required: bool = True
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.EXTERNAL_USE
    approval_rationale: str = (
        "Draft is pending human approval for external use; automatic email sending is disabled."
    )
    external_use_approval_state: ApprovalState = ApprovalState.PENDING
    external_use_allowed: bool = False
    send_enabled: bool = False
    sent: bool = False
    can_send_email: bool = False
    subject: str | None = None
    body: str | None = None
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _copy_legacy_fields(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("email_subject") and values.get("subject"):
                values["email_subject"] = values["subject"]
            if not values.get("email_body") and values.get("body"):
                values["email_body"] = values["body"]
            if not values.get("approval_state") and values.get("approval_status"):
                values["approval_state"] = values["approval_status"]
        return values

    @field_validator("facts_used", mode="before")
    @classmethod
    def _coerce_legacy_facts(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        coerced: list[Any] = []
        for item in value:
            if isinstance(item, str):
                coerced.append(
                    {
                        "claim_text": item,
                        "source_id": "unbacked:legacy_fact",
                        "confidence": 0.0,
                        "claim_type": "unsupported",
                    }
                )
            else:
                coerced.append(item)
        return coerced

    @field_validator("email_body")
    @classmethod
    def _email_body_under_180_words(cls, value: str) -> str:
        if len(value.split()) > 180:
            raise ValueError("email body must be under 180 words")
        return value

    @field_validator("linkedin_note")
    @classmethod
    def _linkedin_note_under_300_characters(cls, value: str) -> str:
        if len(value) > 300:
            raise ValueError("LinkedIn note must be under 300 characters")
        return value

    @field_validator("email_body", "linkedin_note", "email_subject")
    @classmethod
    def _no_em_dash(cls, value: str) -> str:
        if EM_DASH in value:
            raise ValueError("outreach copy must not contain em dashes")
        return value

    @field_validator(
        "blocked_facts",
        "unsupported_claims_flagged",
        "unsupported_claim_explanations",
    )
    @classmethod
    def _safe_internal_lists(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("revision_request")
    @classmethod
    def _safe_revision_request(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("template_id", "template_version", "template_fit_reason")
    @classmethod
    def _safe_template_metadata(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("example_ids_used")
    @classmethod
    def _safe_example_ids(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("approval_required")
    @classmethod
    def _approval_required_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach drafts must require approval")
        return value

    @field_validator("send_enabled", "sent", "can_send_email")
    @classmethod
    def _send_fields_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach drafts must not enable sending")
        return value

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return external_use_state_from_legacy(value)

    @field_validator("external_use_approval_state", mode="before")
    @classmethod
    def _normalize_external_use_state(cls, value: Any) -> ApprovalState:
        return external_use_state_from_legacy(value)

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        scope = normalize_approval_scope(value)
        if scope == ApprovalScope.SEND:
            return ApprovalScope.EXTERNAL_USE
        return scope

    @field_validator("personalization_rationale")
    @classmethod
    def _rationale_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("personalization rationale is required")
        return value

    @model_validator(mode="after")
    def _set_legacy_fields(self) -> OutreachDraft:
        if self.approval_scope != ApprovalScope.EXTERNAL_USE:
            raise ValueError("outreach draft approval scope must be external_use")
        if self.approval_state in {
            ApprovalState.APPROVED_FOR_RESEARCH,
            ApprovalState.APPROVED_FOR_DRAFTING,
        }:
            raise ValueError(
                "outreach draft approval state must not grant research or drafting approval"
            )
        self.external_use_approval_state = self.approval_state
        self.external_use_allowed = state_allows_external_use(self.approval_state)
        if not self.approval_rationale.strip():
            self.approval_rationale = (
                "Draft is pending human approval for external use; automatic email sending "
                "is disabled."
            )
        self.subject = self.email_subject
        self.body = self.email_body
        if self.example_ids_used:
            self.example_guidance_used = True
        if not self.source_ids_used and self.facts_used:
            self.source_ids_used = list(
                dict.fromkeys(
                    fact.source_id
                    for fact in self.facts_used
                    if fact.source_id and not fact.source_id.startswith("unbacked:")
                )
            )
        flagged = [
            f"unbacked outreach fact: {fact.claim_text}"
            for fact in self.facts_used
            if fact.claim_type == "unsupported"
            or fact.confidence <= 0.0
            or not fact.approved
            or fact.source_id.startswith("unbacked:")
        ]
        self.unsupported_claims_flagged = list(
            dict.fromkeys([*self.unsupported_claims_flagged, *flagged])
        )
        generated_explanations = [
            (
                f"{flag} was excluded or requires review because it is not approved "
                "source-backed context."
            )
            for flag in flagged
        ]
        self.unsupported_claim_explanations = list(
            dict.fromkeys([*self.unsupported_claim_explanations, *generated_explanations])
        )
        return self

    @property
    def approval_status(self) -> str:
        return self.approval_state.value


class OutreachDraftVariant(BaseModel):
    """One validated outreach draft variant with a human-readable tone label."""

    variant_label: str = Field(min_length=1)
    draft: OutreachDraft

    @field_validator("variant_label")
    @classmethod
    def _clean_variant_label(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())


class OutreachDraftVariantSet(BaseModel):
    """A small validated set of outreach variants sharing the same approved context."""

    company_name: str = Field(min_length=1)
    outreach_goal: str = ""
    variant_set_summary: str = ""
    requested_variant_labels: list[str] = Field(default_factory=list)
    variants: list[OutreachDraftVariant] = Field(min_length=1, max_length=3)
    approval_required: bool = True
    approval_scope: ApprovalScope = ApprovalScope.EXTERNAL_USE
    send_enabled: bool = False

    @field_validator("requested_variant_labels")
    @classmethod
    def _clean_requested_variant_labels(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                _validate_internal_copy(str(value).strip())
                for value in values
                if str(value).strip()
            )
        )

    @field_validator("variant_set_summary")
    @classmethod
    def _clean_variant_set_summary(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("approval_required")
    @classmethod
    def _variant_set_requires_approval(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach variant sets must require approval")
        return value

    @field_validator("send_enabled")
    @classmethod
    def _variant_set_send_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach variant sets must not enable sending")
        return value

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_variant_set_scope(cls, value: Any) -> ApprovalScope:
        scope = normalize_approval_scope(value)
        if scope == ApprovalScope.SEND:
            return ApprovalScope.EXTERNAL_USE
        return scope

    @model_validator(mode="after")
    def _validate_variant_goal_consistency(self) -> OutreachDraftVariantSet:
        if self.approval_scope != ApprovalScope.EXTERNAL_USE:
            raise ValueError("outreach variant sets must remain external_use only")
        if not self.requested_variant_labels:
            self.requested_variant_labels = [item.variant_label for item in self.variants]
        if len(self.requested_variant_labels) != len(self.variants):
            raise ValueError("requested variant labels must align with the returned variants")
        if not self.variant_set_summary:
            labels = ", ".join(self.requested_variant_labels)
            self.variant_set_summary = (
                f"Generated {len(self.variants)} approval-gated outreach variant(s)"
                f" for {self.company_name}: {labels}. Variants remain no-send drafts."
            )
        return self


class OutreachDraftStatusResult(BaseModel):
    """Structured blocked or clarification-only result for Outreach Composer."""

    status: OutreachDraftStatus
    reason: str = ""
    clarification_request: str = ""
    missing_requirements: list[str] = Field(default_factory=list)
    recommended_next_action: str = ""
    approval_required: bool = True
    approval_scope: ApprovalScope = ApprovalScope.EXTERNAL_USE
    send_enabled: bool = False
    draft_created: bool = False
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)

    @field_validator(
        "reason",
        "clarification_request",
        "recommended_next_action",
    )
    @classmethod
    def _safe_status_text(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())

    @field_validator("missing_requirements")
    @classmethod
    def _safe_missing_requirements(cls, values: list[str]) -> list[str]:
        return [_validate_internal_copy(value.strip()) for value in values if value.strip()]

    @field_validator("approval_required")
    @classmethod
    def _status_result_requires_approval(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach status results must still require approval")
        return value

    @field_validator("send_enabled", "draft_created")
    @classmethod
    def _status_result_read_only(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach status results must not enable send or create drafts")
        return value

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_status_scope(cls, value: Any) -> ApprovalScope:
        scope = normalize_approval_scope(value)
        if scope == ApprovalScope.SEND:
            return ApprovalScope.EXTERNAL_USE
        return scope


class OutreachLLMDraftPayload(BaseModel):
    """Compact LLM draft payload validated into the full OutreachDraft schema."""

    model_config = ConfigDict(extra="ignore")

    company_name: str = ""
    email_subject: str = ""
    email_body: str = ""
    linkedin_note: str = ""
    personalization_rationale: str = ""
    source_ids_used: list[str] = Field(default_factory=list)
    reply_recommended: bool = True
    recommended_next_step: str = ""
    additional_information_needed: list[str] = Field(default_factory=list, max_length=6)
    collaboration_ideas: list[str] = Field(default_factory=list, max_length=4)
    deferral_reason: str = ""


class OutreachLLMDraftVariantPayload(BaseModel):
    """One compact LLM-only outreach variant payload."""

    model_config = ConfigDict(extra="forbid")

    variant_label: str = Field(min_length=1)
    draft: OutreachLLMDraftPayload

    @field_validator("variant_label")
    @classmethod
    def _clean_variant_label(cls, value: str) -> str:
        return _validate_internal_copy(value.strip())


class OutreachLLMVariantSetPayload(BaseModel):
    """Compact LLM-only variant set for one-call outreach tone generation."""

    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    variants: list[OutreachLLMDraftVariantPayload] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _validate_labels_are_unique(self) -> OutreachLLMVariantSetPayload:
        labels = [variant.variant_label for variant in self.variants]
        if len(labels) != len(set(labels)):
            raise ValueError("variant labels must be unique")
        return self


def _draft_payload(draft: OutreachDraft | dict[str, Any]) -> dict[str, Any]:
    if isinstance(draft, OutreachDraft):
        return draft.model_dump(mode="json")
    return dict(draft)


def _draft_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def build_initial_outreach_tracking_record(
    *,
    draft_id: str | int,
    draft: OutreachDraft | dict[str, Any],
    channel: OutreachChannel = "email",
) -> OutreachTrackingRecord:
    """Create the initial manual-only tracking row for a locally saved draft."""

    payload = _draft_payload(draft)
    return OutreachTrackingRecord(
        draft_id=str(draft_id),
        company_name=_draft_text(payload, "company_name"),
        contact_name=_draft_text(payload, "contact_name", "recipient"),
        channel=channel,
        lifecycle_status="draft_pending_approval",
        outreach_sent=False,
        reply_received=False,
        outcome="unknown",
        next_step=(
            "Human review required before any external use; future sent, reply, and "
            "outcome updates are manual only."
        ),
        manual_update_only=True,
        send_enabled=False,
        sent_by_agent=False,
    )
