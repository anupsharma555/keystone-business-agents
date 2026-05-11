"""Schemas for sanitized private outreach example retrieval."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OutreachExampleChannel = Literal["email", "linkedin", "other"]
OutreachExampleStage = Literal[
    "initial_outreach",
    "follow_up",
    "reply",
    "meeting_request",
    "other",
]
OutreachExampleOutcome = Literal[
    "positive_reply",
    "meeting_booked",
    "referral",
    "useful_conversation",
    "success",
    "unknown",
]
OutreachExampleDirection = Literal["outbound", "inbound", "system", "unknown"]


def outreach_example_timestamp() -> str:
    """Return an ISO UTC timestamp for local example records."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def hash_thread_id(thread_id: str) -> str:
    """Return a stable non-reversible thread id hash."""

    return hashlib.sha256(str(thread_id or "").encode("utf-8")).hexdigest()


def _example_id() -> str:
    return f"outreach_example_{uuid4().hex}"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u2014", "-").split()).strip()


def _clean_multiline(value: Any) -> str:
    normalized = str(value or "").replace("\u2014", "-").replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    lines = [" ".join(line.split()).strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _clean_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("field must be a list")
    return list(dict.fromkeys(_clean(item) for item in value if _clean(item)))


_PRIVATE_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"),
    re.compile(
        r"\b(?:from|to|cc|bcc|subject|date|message-id|reply-to|received):\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:api[_-]?key|secret|token|password)\s*[:=]", re.IGNORECASE),
    re.compile(
        r"\bpatient\s+[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:raw body|private raw|medical record number|mrn|date of birth|ssn)\b",
        re.IGNORECASE,
    ),
)


def _reject_private_prompt_context(value: str) -> str:
    if any(pattern.search(value) for pattern in _PRIVATE_CONTEXT_PATTERNS):
        raise ValueError("retrieved outreach examples must be sanitized before prompt use")
    return value


class OutreachExampleMessageSummary(BaseModel):
    """Sanitized summary for one message in a successful outreach thread."""

    model_config = ConfigDict(extra="forbid")

    message_id_hash: str = Field(min_length=12)
    direction: OutreachExampleDirection = "unknown"
    sent_at: str = ""
    sender_role: str = ""
    subject_summary: str = ""
    sanitized_summary: str = Field(min_length=1)
    intent: str = ""
    sensitive_flags: list[str] = Field(default_factory=list)
    raw_body_included: bool = False

    @field_validator(
        "message_id_hash",
        "sent_at",
        "sender_role",
        "subject_summary",
        "sanitized_summary",
        "intent",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("sensitive_flags", mode="before")
    @classmethod
    def _clean_sensitive_flags(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("raw_body_included")
    @classmethod
    def _raw_body_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach examples must not include raw Gmail bodies")
        return value


class OutreachExampleThread(BaseModel):
    """Sanitized thread-level capture record before retrieval indexing."""

    model_config = ConfigDict(extra="forbid")

    source_thread_id_hash: str = Field(min_length=12)
    source_label: str = Field(min_length=1)
    channel: OutreachExampleChannel = "email"
    outcome: OutreachExampleOutcome = "success"
    messages: list[OutreachExampleMessageSummary] = Field(default_factory=list)
    sanitized_thread_summary: str = Field(min_length=1)
    sensitive_flags: list[str] = Field(default_factory=list)
    raw_body_included: bool = False

    @field_validator(
        "source_thread_id_hash",
        "source_label",
        "sanitized_thread_summary",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("sensitive_flags", mode="before")
    @classmethod
    def _clean_sensitive_flags(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("raw_body_included")
    @classmethod
    def _raw_body_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach example threads must not include raw Gmail bodies")
        return value


class OutreachExampleDocument(BaseModel):
    """Approved, sanitized document that may be retrieved for draft guidance."""

    model_config = ConfigDict(extra="forbid")

    example_id: str = Field(default_factory=_example_id, min_length=1)
    source_thread_id_hash: str = Field(min_length=12)
    source_label: str = Field(min_length=1)
    channel: OutreachExampleChannel = "email"
    outreach_stage: OutreachExampleStage = "initial_outreach"
    outcome: OutreachExampleOutcome = "success"
    company_type: str = ""
    opportunity_type: str = ""
    template_id: str | None = None
    style_profile_id: str | None = None
    sanitized_summary: str = Field(min_length=1)
    conversation_pattern: str = Field(min_length=1)
    effective_phrases: list[str] = Field(default_factory=list)
    cta_pattern: str = ""
    follow_up_pattern: str = ""
    reply_pattern: str = ""
    lessons_learned: list[str] = Field(default_factory=list)
    approved_for_drafting: bool = False
    raw_body_included: bool = False
    message_summaries: list[OutreachExampleMessageSummary] = Field(default_factory=list)
    retrieval_text: str = ""
    created_at: str = Field(default_factory=outreach_example_timestamp)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "example_id",
        "source_thread_id_hash",
        "source_label",
        "company_type",
        "opportunity_type",
        "template_id",
        "style_profile_id",
        "sanitized_summary",
        "conversation_pattern",
        "cta_pattern",
        "follow_up_pattern",
        "reply_pattern",
        "created_at",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _clean_multiline(value)

    @field_validator("effective_phrases", "lessons_learned", mode="before")
    @classmethod
    def _clean_text_lists(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("raw_body_included")
    @classmethod
    def _raw_body_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach example documents must not include raw Gmail bodies")
        return value

    @model_validator(mode="after")
    def _build_retrieval_text(self) -> OutreachExampleDocument:
        if not self.retrieval_text:
            self.retrieval_text = self.safe_retrieval_text()
        return self

    def safe_retrieval_text(self) -> str:
        """Return prompt-safe searchable text derived only from sanitized fields."""

        parts = [
            self.source_label,
            self.channel,
            self.outreach_stage,
            self.outcome,
            self.company_type,
            self.opportunity_type,
            self.sanitized_summary,
            self.conversation_pattern,
            " ".join(self.effective_phrases),
            self.cta_pattern,
            self.follow_up_pattern,
            self.reply_pattern,
            " ".join(self.lessons_learned),
        ]
        return _clean(" ".join(part for part in parts if part))

    def prompt_context(self) -> dict[str, Any]:
        """Return approved example context safe to pass into drafting prompts."""

        return {
            "example_id": self.example_id,
            "source_label": self.source_label,
            "channel": self.channel,
            "outreach_stage": self.outreach_stage,
            "outcome": self.outcome,
            "company_type": self.company_type,
            "opportunity_type": self.opportunity_type,
            "template_id": self.template_id,
            "style_profile_id": self.style_profile_id,
            "sanitized_summary": self.sanitized_summary,
            "conversation_pattern": self.conversation_pattern,
            "effective_phrases": self.effective_phrases,
            "cta_pattern": self.cta_pattern,
            "follow_up_pattern": self.follow_up_pattern,
            "reply_pattern": self.reply_pattern,
            "lessons_learned": self.lessons_learned,
            "approved_for_drafting": self.approved_for_drafting,
            "raw_body_included": False,
        }


class OutreachExampleRecord(BaseModel):
    """Backward-compatible sanitized outreach example record used by local memory."""

    model_config = ConfigDict(extra="forbid")

    example_id: str = Field(min_length=1)
    approved_for_drafting: bool = False
    company_types: list[str] = Field(default_factory=list)
    opportunity_types: list[str] = Field(default_factory=list)
    outreach_goals: list[str] = Field(default_factory=list)
    outreach_stages: list[str] = Field(default_factory=list)
    conversation_pattern: str = Field(min_length=1)
    effective_phrases: list[str] = Field(default_factory=list)
    cta_pattern: str = ""
    follow_up_pattern: str = ""
    reply_pattern: str = ""
    lessons_learned: list[str] = Field(default_factory=list)
    template_id: str | None = None
    style_profile_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    raw_body_included: bool = False

    @field_validator(
        "example_id",
        "conversation_pattern",
        "cta_pattern",
        "follow_up_pattern",
        "reply_pattern",
        "template_id",
        "style_profile_id",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _reject_private_prompt_context(_clean_multiline(value))

    @field_validator(
        "company_types",
        "opportunity_types",
        "outreach_goals",
        "outreach_stages",
        "effective_phrases",
        "lessons_learned",
        "source_ids",
        mode="before",
    )
    @classmethod
    def _clean_text_lists(cls, value: Any) -> list[str]:
        return [_reject_private_prompt_context(item) for item in _clean_list(value)]

    @field_validator("raw_body_included")
    @classmethod
    def _raw_body_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach example records must not include raw bodies")
        return value

    def retrieval_text(self) -> str:
        """Return sanitized retrieval text for memory indexing."""

        return _clean(
            " ".join(
                [
                    self.example_id,
                    " ".join(self.company_types),
                    " ".join(self.opportunity_types),
                    " ".join(self.outreach_goals),
                    " ".join(self.outreach_stages),
                    self.conversation_pattern,
                    " ".join(self.effective_phrases),
                    self.cta_pattern,
                    self.follow_up_pattern,
                    self.reply_pattern,
                    " ".join(self.lessons_learned),
                    self.template_id or "",
                    self.style_profile_id or "",
                ]
            )
        )


class RetrievedOutreachExample(BaseModel):
    """Prompt-safe retrieved outreach example with deterministic match metadata."""

    model_config = ConfigDict(extra="forbid")

    example_id: str = Field(min_length=1)
    match_score: int = Field(default=0, ge=0)
    match_reason: str = ""
    conversation_pattern: str = Field(min_length=1)
    effective_phrases: list[str] = Field(default_factory=list)
    cta_pattern: str = ""
    follow_up_pattern: str = ""
    reply_pattern: str = ""
    lessons_learned: list[str] = Field(default_factory=list)
    template_id: str | None = None
    style_profile_id: str | None = None
    raw_body_included: bool = False

    @field_validator(
        "example_id",
        "match_reason",
        "conversation_pattern",
        "cta_pattern",
        "follow_up_pattern",
        "reply_pattern",
        "template_id",
        "style_profile_id",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _clean_multiline(value)

    @field_validator("effective_phrases", "lessons_learned", mode="before")
    @classmethod
    def _clean_text_lists(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("raw_body_included")
    @classmethod
    def _raw_body_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("retrieved outreach examples must not include raw bodies")
        return value


class OutreachExampleRetrievalResult(BaseModel):
    """Local retrieval envelope for approved outreach example documents."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "local_outreach_examples"
    query: str = ""
    approved_only: bool = True
    examples: list[OutreachExampleDocument] = Field(default_factory=list)
    records: list[RetrievedOutreachExample] = Field(default_factory=list)
    guidance: str = ""
    raw_body_included: bool = False
    embeddings_used: bool = False
    external_vector_db_used: bool = False
    send_enabled: bool = False

    @field_validator("query", mode="before")
    @classmethod
    def _clean_query(cls, value: Any) -> str:
        return _clean(value)

    @field_validator(
        "raw_body_included",
        "embeddings_used",
        "external_vector_db_used",
        "send_enabled",
    )
    @classmethod
    def _unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("outreach example retrieval must remain local and non-sending")
        return value

    @field_validator("approved_only")
    @classmethod
    def _approved_only_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("outreach example retrieval requires approved examples only")
        return value
