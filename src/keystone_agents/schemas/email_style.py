"""Aggregate email style profile schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.approval import (
    ApprovalScope,
    ApprovalState,
    normalize_approval_scope,
    normalize_approval_state,
    state_allows_drafting,
)

StyleDirectness = Literal["low", "medium", "high"]
StyleFormality = Literal["casual", "neutral", "professional", "formal"]
StyleCTA = Literal["soft_question", "direct_question", "calendar_offer", "context_request"]
StyleSentenceLength = Literal["short", "medium", "varied"]

_MAX_SNIPPET_WORDS = 60
_MAX_SNIPPET_CHARS = 360


def _clean(value: str) -> str:
    return " ".join(value.replace("\u2014", "-").split()).strip()


def _clean_multiline(value: str) -> str:
    normalized = value.replace("\u2014", "-").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()).strip() for line in normalized.split("\n")]
    return "\n".join(line for line in lines if line).strip()


class EmailStyleProfile(BaseModel):
    """Approved aggregate style features for draft-only email personalization.

    This schema stores style preferences and short approved snippets only. It must not contain
    raw sent-email bodies or enable sending.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(default="default", min_length=1)
    source: str = "fixture"
    source_id: str = "fixture:email_style_profile"
    source_url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    greeting_patterns: list[str] = Field(default_factory=list)
    signoffs: list[str] = Field(default_factory=list)
    sentence_length: StyleSentenceLength = "medium"
    average_sentence_words: int = Field(default=16, ge=4, le=40)
    directness: StyleDirectness = "medium"
    cta_style: StyleCTA = "soft_question"
    formality: StyleFormality = "professional"
    formatting_preferences: list[str] = Field(default_factory=list)
    preferred_phrases: list[str] = Field(default_factory=list)
    avoided_phrases: list[str] = Field(default_factory=list)
    approved_sample_snippets: list[str] = Field(default_factory=list)
    notes: str = ""
    raw_sent_email_bodies_included: bool = False
    send_enabled: bool = False
    sent: bool = False

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("approval_scope", mode="before")
    @classmethod
    def _normalize_approval_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @field_validator(
        "profile_id",
        "source",
        "source_id",
        "source_url",
        "notes",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return _clean(str(value))

    @field_validator(
        "greeting_patterns",
        "formatting_preferences",
        "preferred_phrases",
        "avoided_phrases",
        mode="before",
    )
    @classmethod
    def _clean_text_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("style profile list fields must be lists")
        return list(dict.fromkeys(_clean(str(item)) for item in value if _clean(str(item))))

    @field_validator("signoffs", mode="before")
    @classmethod
    def _clean_signoffs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("style profile signoffs must be lists")
        return list(
            dict.fromkeys(cleaned for item in value if (cleaned := _clean_multiline(str(item))))
        )

    @field_validator("approved_sample_snippets", mode="before")
    @classmethod
    def _clean_approved_snippets(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("approved_sample_snippets must be a list")
        snippets = []
        for item in value:
            snippet = _clean(str(item))
            if not snippet:
                continue
            if len(snippet) > _MAX_SNIPPET_CHARS or len(snippet.split()) > _MAX_SNIPPET_WORDS:
                raise ValueError(
                    "approved sample snippets must be short excerpts, not raw email bodies"
                )
            snippets.append(snippet)
        return list(dict.fromkeys(snippets))

    @field_validator("raw_sent_email_bodies_included", "send_enabled", "sent")
    @classmethod
    def _privacy_and_side_effect_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("email style profiles must not store raw bodies or enable sending")
        return value

    @model_validator(mode="after")
    def _validate_profile(self) -> EmailStyleProfile:
        if self.approval_scope != ApprovalScope.DRAFTING:
            raise ValueError("email style profiles are only approved for drafting guidance")
        if not self.source_url:
            self.source_url = (
                self.source
                if self.source.startswith("fixture://")
                else f"fixture://{self.source_id}"
            )
        return self

    @property
    def approved_for_drafting(self) -> bool:
        return state_allows_drafting(self.approval_state)

    def safe_prompt_context(self) -> dict[str, Any]:
        """Return aggregate style context safe for prompts and reports."""

        return {
            "profile_id": self.profile_id,
            "source": self.source,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "approval_state": self.approval_state.value,
            "approval_scope": self.approval_scope.value,
            "greeting_patterns": self.greeting_patterns,
            "signoffs": self.signoffs,
            "sentence_length": self.sentence_length,
            "average_sentence_words": self.average_sentence_words,
            "directness": self.directness,
            "cta_style": self.cta_style,
            "formality": self.formality,
            "formatting_preferences": self.formatting_preferences,
            "preferred_phrases": self.preferred_phrases,
            "avoided_phrases": self.avoided_phrases,
            "approved_sample_snippets": self.approved_sample_snippets,
            "raw_sent_email_bodies_included": False,
            "send_enabled": False,
        }


class EmailStyleSampleSummary(BaseModel):
    """Redacted metadata for one sent-email sample used to infer aggregate style."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    source_url: str = ""
    subject_summary: str = ""
    body_hash: str = Field(min_length=12)
    body_length: int = Field(default=0, ge=0)
    redacted_summary: str = ""
    sensitive_flags: list[str] = Field(default_factory=list)
    used_for_profile: bool = True

    @field_validator(
        "source_id",
        "source_url",
        "subject_summary",
        "body_hash",
        "redacted_summary",
        mode="before",
    )
    @classmethod
    def _clean_summary_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return _clean(str(value))

    @field_validator("sensitive_flags", mode="before")
    @classmethod
    def _clean_flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("sensitive_flags must be a list")
        return list(dict.fromkeys(_clean(str(item)) for item in value if _clean(str(item))))


class EmailStyleProfileBuildResult(BaseModel):
    """Safe profiling result. Raw sent bodies are never included."""

    model_config = ConfigDict(extra="forbid")

    profile: EmailStyleProfile
    sample_summaries: list[EmailStyleSampleSummary] = Field(default_factory=list)
    sample_count: int = Field(default=0, ge=0)
    usable_sample_count: int = Field(default=0, ge=0)
    excluded_sample_count: int = Field(default=0, ge=0)
    approval_required: bool = True
    approved_for_use: bool = False
    raw_sent_email_bodies_included: bool = False
    send_enabled: bool = False
    sent: bool = False
    limitations: list[str] = Field(default_factory=list)

    @field_validator("raw_sent_email_bodies_included", "send_enabled", "sent")
    @classmethod
    def _side_effect_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("style profiling results must not include raw bodies or sending")
        return value

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_limitations(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("limitations must be a list")
        return list(dict.fromkeys(_clean(str(item)) for item in value if _clean(str(item))))

    @model_validator(mode="after")
    def _approval_matches_profile(self) -> EmailStyleProfileBuildResult:
        if self.sample_summaries:
            self.sample_count = len(self.sample_summaries)
            self.usable_sample_count = sum(
                1 for summary in self.sample_summaries if summary.used_for_profile
            )
            self.excluded_sample_count = self.sample_count - self.usable_sample_count
        self.approved_for_use = self.profile.approved_for_drafting
        self.approval_required = not self.approved_for_use
        return self
