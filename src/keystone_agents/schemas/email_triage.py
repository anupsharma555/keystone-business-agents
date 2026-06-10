"""Structured output schema for Gmail inbound triage."""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

EmailCategory = Literal[
    "consulting_opportunity",
    "collaboration_opportunity",
    "vendor",
    "suspicious",
    "newsletter",
    "unrelated",
]
EmailPriority = Literal["low", "normal", "high", "urgent"]
GmailPriorityBucket = Literal["urgent", "important", "can_wait", "ignore"]
EmailRiskFlag = Literal[
    "finance",
    "legal",
    "security",
    "phi",
    "suspicious",
    "contractual",
    "possible_phi",
    "legal_review",
    "finance_review",
    "credential_context",
    "professional_advice",
    "unsupported_claim",
]
GmailRequestStatus = Literal["clarification_required", "blocked"]

KEYSTONE_TRIAGE_LABEL = "Keystone/Triage"
GMAIL_PRIMARY_CATEGORY_LABELS: dict[EmailCategory, str] = {
    "consulting_opportunity": "Keystone/Consulting Opportunity",
    "collaboration_opportunity": "Keystone/Collaboration",
    "vendor": "Keystone/Vendor",
    "suspicious": "Keystone/Suspicious",
    "newsletter": "Keystone/Newsletter",
    "unrelated": "Keystone/Other",
}
GMAIL_OVERLAY_LABELS = {
    "action_required": "Keystone/Action Required",
    "manual_review": "Keystone/Manual Review",
    "security_review": "Keystone/Security Review",
    "phi_blocked": "Keystone/PHI Blocked",
    "approval_needed": "Keystone/Draft Pending Approval",
    "finance_review": "Keystone/Finance Review",
    "legal_review": "Keystone/Legal Review",
}
GMAIL_MANAGED_LABELS = tuple(
    dict.fromkeys(
        [
            KEYSTONE_TRIAGE_LABEL,
            *GMAIL_PRIMARY_CATEGORY_LABELS.values(),
            *GMAIL_OVERLAY_LABELS.values(),
        ]
    )
)
GMAIL_PRIMARY_LABEL_SET = frozenset(GMAIL_PRIMARY_CATEGORY_LABELS.values())


def managed_gmail_labels(
    *,
    category: EmailCategory,
    needs_reply: bool,
    risk_flags: list[str],
    approval_required: bool = False,
) -> list[str]:
    """Return Keystone-managed Gmail labels with one primary category label."""

    labels = [KEYSTONE_TRIAGE_LABEL, GMAIL_PRIMARY_CATEGORY_LABELS[category]]
    if needs_reply:
        labels.append(GMAIL_OVERLAY_LABELS["action_required"])
    if approval_required:
        labels.append(GMAIL_OVERLAY_LABELS["approval_needed"])
    if risk_flags:
        labels.append(GMAIL_OVERLAY_LABELS["manual_review"])
    if "security" in risk_flags or category == "suspicious":
        labels.append(GMAIL_OVERLAY_LABELS["security_review"])
    if "possible_phi" in risk_flags or "phi" in risk_flags:
        labels.append(GMAIL_OVERLAY_LABELS["phi_blocked"])
    if "finance_review" in risk_flags or "finance" in risk_flags:
        labels.append(GMAIL_OVERLAY_LABELS["finance_review"])
    if "legal_review" in risk_flags or "legal" in risk_flags or "contractual" in risk_flags:
        labels.append(GMAIL_OVERLAY_LABELS["legal_review"])
    return list(dict.fromkeys(labels))


def normalize_managed_gmail_labels(labels: list[str]) -> list[str]:
    """Keep one primary category label and preserve overlay labels."""

    clean_labels = [label.strip() for label in labels if label.strip()]
    primary_seen = False
    normalized: list[str] = []
    for label in clean_labels:
        if label in GMAIL_PRIMARY_LABEL_SET:
            if primary_seen:
                continue
            primary_seen = True
        normalized.append(label)
    return list(dict.fromkeys(normalized))


class GmailLinkRecord(BaseModel):
    """One extracted URL from a Gmail message body or HTML link attribute."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    domain: str = ""
    suspicious: bool = False
    reasons: list[str] = Field(default_factory=list)

    @field_validator("url", "domain")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("reasons")
    @classmethod
    def normalize_reasons(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def populate_domain(self) -> GmailLinkRecord:
        if not self.domain:
            parsed = urlparse(self.url if "://" in self.url else f"https://{self.url}")
            self.domain = (parsed.netloc or "").lower()
        return self


class GmailAttachmentMetadata(BaseModel):
    """Attachment metadata only. Attachment bodies are never ingested."""

    model_config = ConfigDict(extra="forbid")

    filename: str = ""
    mime_type: str = ""
    size_bytes: int = Field(default=0, ge=0)
    attachment_id_present: bool = False
    risk_flags: list[str] = Field(default_factory=list)

    @field_validator("filename", "mime_type")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("risk_flags")
    @classmethod
    def normalize_risk_flags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class GmailMessageEnvelope(BaseModel):
    """Sanitized, LLM-ready Gmail message context."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = ""
    thread_id: str = ""
    received_at: str = ""
    sender_name: str = ""
    sender_email: str = ""
    to: str = ""
    subject: str = ""
    snippet: str = ""
    prior_labels: list[str] = Field(default_factory=list)
    normalized_body: str = ""
    extracted_links: list[GmailLinkRecord] = Field(default_factory=list)
    attachment_metadata: list[GmailAttachmentMetadata] = Field(default_factory=list)
    thread_summary: str = ""
    thread_context: str = ""
    thread_message_count: int = Field(default=1, ge=0)
    suspicious_signals: list[str] = Field(default_factory=list)
    triage_limitations: list[str] = Field(default_factory=list)

    @field_validator(
        "message_id",
        "thread_id",
        "received_at",
        "sender_name",
        "sender_email",
        "to",
        "subject",
        "snippet",
        "normalized_body",
        "thread_summary",
        "thread_context",
    )
    @classmethod
    def clean_text_fields(cls, value: str) -> str:
        return " ".join(value.replace("\u2014", "-").split())

    @field_validator("prior_labels", "suspicious_signals", "triage_limitations")
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class EmailTriageResult(BaseModel):
    """Pydantic output model for the Gmail triage agent."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = ""
    thread_id: str = ""
    received_at: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    category: EmailCategory
    confidence: float = Field(ge=0.0, le=1.0)
    priority: EmailPriority = "normal"
    summary: str = ""
    thread_summary: str = ""
    thread_context: str = ""
    reasoning: str
    needs_reply: bool = False
    recommended_labels: list[str] = Field(default_factory=list)
    risk_flags: list[EmailRiskFlag] = Field(default_factory=list)
    suspicious_signals: list[str] = Field(default_factory=list)
    recommended_next_agent: str = "human_review"
    triage_limitations: list[str] = Field(default_factory=list)
    prior_labels: list[str] = Field(default_factory=list)
    snippet: str = ""
    normalized_body: str = ""
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)
    extracted_links: list[GmailLinkRecord] = Field(default_factory=list)
    attachment_metadata: list[GmailAttachmentMetadata] = Field(default_factory=list)
    recommended_action: str
    draft_reply: str | None = None
    draft_created: bool = False
    style_profile_used: bool = False
    style_profile_id: str = ""
    approval_required: bool = False
    requires_human_review: bool = True

    @field_validator(
        "subject",
        "summary",
        "thread_summary",
        "thread_context",
        "reasoning",
        "recommended_action",
        "recommended_next_agent",
        "draft_reply",
        "style_profile_id",
        "snippet",
        "normalized_body",
    )
    @classmethod
    def reject_em_dash(cls, value: str | None) -> str | None:
        if value is not None and "\u2014" in value:
            raise ValueError("Email triage output must not contain em dashes.")
        return value

    @field_validator("suspicious_signals", "triage_limitations", "prior_labels")
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def require_approval_for_drafts(self) -> EmailTriageResult:
        if self.draft_reply:
            if not self.needs_reply:
                raise ValueError("Draft replies require needs_reply=true.")
            if not self.approval_required:
                raise ValueError("Draft replies require approval_required=true.")
        self.recommended_labels = normalize_managed_gmail_labels(self.recommended_labels)
        return self


EmailTriage = EmailTriageResult


class GmailThreadSummaryMessage(BaseModel):
    """Sanitized per-message summary item for a Gmail thread review."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = ""
    received_at: str = ""
    sender_name: str = ""
    sender_email: str = ""
    subject: str = ""
    snippet: str = ""
    prior_labels: list[str] = Field(default_factory=list)
    summary: str = ""

    @field_validator(
        "message_id",
        "received_at",
        "sender_name",
        "sender_email",
        "subject",
        "snippet",
        "summary",
    )
    @classmethod
    def clean_text_fields(cls, value: str) -> str:
        if "\u2014" in value:
            raise ValueError("Gmail thread summary output must not contain em dashes.")
        return " ".join(value.split())

    @field_validator("prior_labels")
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class GmailThreadSummaryResult(BaseModel):
    """Read-only deterministic summary of a selected Gmail thread."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["thread_summary"] = "thread_summary"
    thread_id: str = ""
    source_label: str = ""
    query: str = ""
    subject: str = ""
    summary: str = ""
    thread_context: str = ""
    message_count: int = Field(default=0, ge=0)
    latest_received_at: str = ""
    participants: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    deadlines: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    triage_limitations: list[str] = Field(default_factory=list)
    messages: list[GmailThreadSummaryMessage] = Field(default_factory=list)
    send_enabled: bool = False
    draft_created: bool = False
    labels_modified: bool = False

    @field_validator(
        "thread_id",
        "source_label",
        "query",
        "subject",
        "summary",
        "thread_context",
        "latest_received_at",
    )
    @classmethod
    def clean_text_fields(cls, value: str) -> str:
        if "\u2014" in value:
            raise ValueError("Gmail thread summary output must not contain em dashes.")
        return " ".join(value.split())

    @field_validator(
        "participants",
        "action_items",
        "deadlines",
        "open_questions",
        "triage_limitations",
    )
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            text = " ".join(value.split())
            if "\u2014" in text:
                raise ValueError("Gmail thread summary output must not contain em dashes.")
            if text:
                cleaned.append(text)
        return list(dict.fromkeys(cleaned))

    @field_validator("send_enabled", "draft_created", "labels_modified")
    @classmethod
    def reject_mutation_state(cls, value: bool) -> bool:
        if value:
            raise ValueError("Gmail thread summary must remain read-only.")
        return value


class GmailClarificationResult(BaseModel):
    """Structured clarification or block output for unsafe Gmail CLI requests."""

    model_config = ConfigDict(extra="forbid")

    status: GmailRequestStatus
    clarification_request: str = ""
    operation: str = ""
    reason_code: str = ""
    message: str = ""
    source_label: str = ""
    query: str = ""
    candidate_count: int = Field(default=0, ge=0)
    candidate_thread_ids: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    suggested_next_steps: list[str] = Field(default_factory=list)
    send_enabled: bool = False
    draft_created: bool = False
    labels_modified: bool = False

    @field_validator(
        "clarification_request",
        "operation",
        "reason_code",
        "message",
        "source_label",
        "query",
    )
    @classmethod
    def clean_text_fields(cls, value: str) -> str:
        if "\u2014" in value:
            raise ValueError("Gmail clarification output must not contain em dashes.")
        return " ".join(value.split())

    @field_validator("candidate_thread_ids", "missing_inputs", "suggested_next_steps")
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            text = " ".join(value.split())
            if "\u2014" in text:
                raise ValueError("Gmail clarification output must not contain em dashes.")
            if text:
                cleaned.append(text)
        return list(dict.fromkeys(cleaned))

    @field_validator("send_enabled", "draft_created", "labels_modified")
    @classmethod
    def reject_mutation_state(cls, value: bool) -> bool:
        if value:
            raise ValueError("Structured Gmail block output must not enable mutations.")
        return value


class GmailPriorityGroupedMessage(BaseModel):
    """One message assigned to a GT-1 priority bucket by the Gmail LLM path."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = ""
    thread_id: str = ""
    received_at: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    bucket: GmailPriorityBucket
    category: EmailCategory
    confidence: float = Field(ge=0.0, le=1.0)
    priority: EmailPriority = "normal"
    summary: str = ""
    reasoning: str
    needs_reply: bool = False
    recommended_action: str
    recommended_labels: list[str] = Field(default_factory=list)
    risk_flags: list[EmailRiskFlag] = Field(default_factory=list)
    draft_reply: str | None = None
    draft_created: bool = False
    approval_required: bool = False
    requires_human_review: bool = True
    send_enabled: bool = False
    sent: bool = False

    @field_validator(
        "message_id",
        "thread_id",
        "received_at",
        "subject",
        "sender_name",
        "sender_email",
        "summary",
        "reasoning",
        "recommended_action",
        "draft_reply",
    )
    @classmethod
    def reject_em_dash(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return value.replace("\u2014", "-")

    @field_validator("send_enabled", "sent")
    @classmethod
    def reject_send_state(cls, value: bool) -> bool:
        if value:
            raise ValueError("Gmail priority grouping must not enable or report sending.")
        return value

    @model_validator(mode="after")
    def require_urgent_only_drafts(self) -> GmailPriorityGroupedMessage:
        self.recommended_labels = normalize_managed_gmail_labels(self.recommended_labels)
        if self.bucket != "urgent" and (self.draft_reply or self.draft_created):
            raise ValueError("Only urgent messages may include draft replies.")
        if self.draft_reply:
            if not self.needs_reply:
                raise ValueError("Draft replies require needs_reply=true.")
            if not self.draft_created:
                raise ValueError("Draft replies require draft_created=true.")
            if not self.approval_required:
                raise ValueError("Draft replies require approval_required=true.")
        if self.draft_created and not self.draft_reply:
            raise ValueError("draft_created=true requires draft_reply content.")
        return self


class GmailPriorityGroupingResult(BaseModel):
    """Batch Gmail priority grouping for the GT-1 improvement test."""

    model_config = ConfigDict(extra="forbid")

    request_summary: str = ""
    source_label: str = "UNREAD"
    lookback_days: int = Field(default=3, ge=1, le=30)
    source_message_count: int = Field(default=0, ge=0)
    urgent: list[GmailPriorityGroupedMessage] = Field(default_factory=list)
    important: list[GmailPriorityGroupedMessage] = Field(default_factory=list)
    can_wait: list[GmailPriorityGroupedMessage] = Field(default_factory=list)
    ignore: list[GmailPriorityGroupedMessage] = Field(default_factory=list)
    draft_count: int = Field(default=0, ge=0)
    send_enabled: bool = False
    sent: bool = False
    live_side_effects_enabled: bool = False
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("request_summary", "source_label")
    @classmethod
    def clean_text_fields(cls, value: str) -> str:
        return " ".join(value.replace("\u2014", "-").split())

    @field_validator("audit_notes")
    @classmethod
    def clean_audit_notes(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            text = " ".join(value.replace("\u2014", "-").split())
            if text:
                cleaned.append(text)
        return list(dict.fromkeys(cleaned))

    @field_validator("send_enabled", "sent", "live_side_effects_enabled")
    @classmethod
    def reject_live_side_effect_state(cls, value: bool) -> bool:
        if value:
            raise ValueError("Gmail priority grouping must not enable live side effects.")
        return value

    @model_validator(mode="after")
    def validate_bucket_membership_and_drafts(self) -> GmailPriorityGroupingResult:
        for bucket_name in ("urgent", "important", "can_wait", "ignore"):
            for message in getattr(self, bucket_name):
                if message.bucket != bucket_name:
                    raise ValueError(
                        f"Message {message.message_id or '(unknown)'} has bucket "
                        f"{message.bucket!r} inside {bucket_name!r}."
                    )
                if bucket_name != "urgent" and (message.draft_reply or message.draft_created):
                    raise ValueError("Draft replies are only allowed in the urgent bucket.")

        self.draft_count = sum(1 for message in self.urgent if message.draft_created)
        if not self.source_message_count:
            self.source_message_count = sum(
                len(getattr(self, bucket_name))
                for bucket_name in ("urgent", "important", "can_wait", "ignore")
            )
        return self
