"""Compact human review card schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EM_DASH = "\u2014"
ReviewObjectType = Literal[
    "gmail_triage",
    "gmail_draft",
    "company_profile",
    "opportunity",
    "outreach_draft",
    "pipeline",
    "approval",
    "other",
]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace(EM_DASH, "-").split()).strip()


class ReviewEvidenceItem(BaseModel):
    """One concise evidence item for human review."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    source_id: str = ""
    source_url: str = ""
    confidence: str = ""

    @field_validator("text", "source_id", "source_url", "confidence", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)


class ReviewSourceItem(BaseModel):
    """One compact source attribution item."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = ""
    title: str = ""
    url: str = ""

    @field_validator("source_id", "title", "url", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)


class ReviewCard(BaseModel):
    """Short review artifact for terminal, markdown, and Slack surfaces."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    object_type: ReviewObjectType = "other"
    object_id: str = ""
    decision_summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[ReviewEvidenceItem] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    approval_required: bool = True
    approval_status: str = "pending"
    approval_scope: str = ""
    next_action: str = Field(min_length=1)
    sources: list[ReviewSourceItem] = Field(default_factory=list)
    outbound_copy: bool = False
    approval_warning: str = "Draft only, human approval required before outbound communication."

    @field_validator(
        "title",
        "object_id",
        "decision_summary",
        "reason",
        "approval_status",
        "approval_scope",
        "next_action",
        "approval_warning",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("risks", mode="before")
    @classmethod
    def _clean_risks(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [values]
        return list(dict.fromkeys(item for raw in values if (item := _clean(raw))))


__all__ = [
    "ReviewCard",
    "ReviewEvidenceItem",
    "ReviewObjectType",
    "ReviewSourceItem",
]
