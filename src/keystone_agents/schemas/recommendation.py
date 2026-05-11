"""Recommendation intake and generalized opportunity entity schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from keystone_agents.schemas.approval import ApprovalState, normalize_approval_state

OpportunityEntityType = Literal[
    "company",
    "person",
    "institute",
    "lab",
    "conference",
    "grant",
    "accelerator",
    "rfp",
    "funder",
    "publication_group",
    "other",
]

RecommendationDecision = Literal["pursue", "maybe", "archive", "needs_more_research"]
RecommendationNextStep = Literal[
    "account_research",
    "contact_research",
    "draft_email",
    "draft_linkedin",
    "monitor",
    "archive",
]
ContactPathType = Literal["email", "linkedin", "website", "form", "conference_portal", "other"]


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u2014", "-").split()).strip()


def _clean_url(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://", "mailto:", "fixture://", "local://")):
        return text
    if "." in text and " " not in text:
        return f"https://{text}"
    return text


class OpportunityContactPath(BaseModel):
    """One possible way to reach or research a generalized opportunity entity."""

    path_type: ContactPathType = "other"
    label: str = ""
    value: str = ""
    url: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_id: str = ""
    needs_confirmation: bool = True

    @field_validator("label", "value", "source_id", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("url", mode="before")
    @classmethod
    def clean_url(cls, value: Any) -> str:
        return _clean_url(value)

    @model_validator(mode="after")
    def sync_value_and_confirmation(self) -> OpportunityContactPath:
        if not self.value and self.url:
            self.value = self.url
        if self.value and self.confidence >= 0.75:
            self.needs_confirmation = False
        return self


class OpportunityEntity(BaseModel):
    """A company, person, institute, conference, grant, or other outreach-relevant entity."""

    entity_type: OpportunityEntityType = "other"
    name: str = Field(min_length=1)
    canonical_key: str = ""
    url: str = ""
    description: str = ""
    relevance_summary: str = ""
    timing_signal: str = ""
    location: str = ""
    contact_paths: list[OpportunityContactPath] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    approval_state: ApprovalState = ApprovalState.PENDING

    @field_validator("name", "canonical_key", "description", "relevance_summary", mode="before")
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("url", mode="before")
    @classmethod
    def clean_url(cls, value: Any) -> str:
        return _clean_url(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def clean_source_ids(cls, values: Any) -> list[str]:
        return list(dict.fromkeys(_clean(value) for value in values or [] if _clean(value)))

    @field_validator("source_urls", mode="before")
    @classmethod
    def clean_source_urls(cls, values: Any) -> list[str]:
        return list(dict.fromkeys(_clean_url(value) for value in values or [] if _clean_url(value)))

    @field_validator("approval_state", mode="before")
    @classmethod
    def clean_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @model_validator(mode="after")
    def populate_key_and_sources(self) -> OpportunityEntity:
        if not self.canonical_key:
            self.canonical_key = " ".join(
                f"{self.entity_type}:{self.name}".lower().replace("_", " ").split()
            )
        if self.url and self.url not in self.source_urls:
            self.source_urls.insert(0, self.url)
        return self


class RecommendationScore(BaseModel):
    """Transparent scoring for deciding whether a recommendation is worth pursuit."""

    fit_score: int = Field(default=0, ge=0, le=100)
    timing_score: int = Field(default=0, ge=0, le=100)
    evidence_score: int = Field(default=0, ge=0, le=100)
    contactability_score: int = Field(default=0, ge=0, le=100)
    effort_score: int = Field(default=50, ge=0, le=100)
    priority_score: int = Field(default=0, ge=0, le=100)
    rationale: str = ""

    @field_validator("rationale", mode="before")
    @classmethod
    def clean_rationale(cls, value: Any) -> str:
        return _clean(value)

    @model_validator(mode="after")
    def populate_priority(self) -> RecommendationScore:
        if self.priority_score == 0:
            self.priority_score = round(
                self.fit_score * 0.35
                + self.timing_score * 0.2
                + self.evidence_score * 0.2
                + self.contactability_score * 0.15
                + self.effort_score * 0.1
            )
        return self


class RecommendationIntakeResult(BaseModel):
    """Decision packet for an operator-provided recommendation or candidate lead."""

    input_text: str
    entity: OpportunityEntity
    decision: RecommendationDecision = "needs_more_research"
    recommended_next_step: RecommendationNextStep = "account_research"
    score: RecommendationScore = Field(default_factory=RecommendationScore)
    why_relevant: str = ""
    evidence_summary: str = ""
    unknowns: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    suggested_searches: list[str] = Field(default_factory=list)
    source_links: list[str] = Field(default_factory=list)
    approval_required: bool = True
    send_enabled: bool = False
    sent: bool = False

    @field_validator(
        "input_text",
        "why_relevant",
        "evidence_summary",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("unknowns", "risks", "suggested_searches", mode="before")
    @classmethod
    def clean_list(cls, values: Any) -> list[str]:
        return list(dict.fromkeys(_clean(value) for value in values or [] if _clean(value)))

    @field_validator("source_links", mode="before")
    @classmethod
    def clean_links(cls, values: Any) -> list[str]:
        return list(dict.fromkeys(_clean_url(value) for value in values or [] if _clean_url(value)))

    @field_validator("send_enabled", "sent")
    @classmethod
    def unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("recommendation intake cannot send or enable sending")
        return value

    @model_validator(mode="after")
    def align_decision_and_next_step(self) -> RecommendationIntakeResult:
        if self.decision == "archive":
            self.recommended_next_step = "archive"
        elif self.decision == "needs_more_research" and self.recommended_next_step in {
            "draft_email",
            "draft_linkedin",
        }:
            self.recommended_next_step = "account_research"
        return self
