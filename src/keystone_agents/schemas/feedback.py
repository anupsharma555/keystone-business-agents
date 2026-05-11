"""Human feedback schemas for agent output review."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from keystone_agents.schemas.approval import approval_timestamp

FeedbackObjectType = Literal[
    "email_triage",
    "company_profile",
    "opportunity",
    "outreach_draft",
    "digest",
    "other",
]
FeedbackRating = Literal["good", "okay", "poor"]
FeedbackReviewStage = Literal["pipeline_review", "approval_review", "post_run_review"]

GENERAL_FEEDBACK_TAGS = (
    "too_salesy",
    "weak_sourcing",
    "wrong_target",
    "good_fit",
    "too_verbose",
    "too_generic",
    "unsafe_claim",
    "needs_more_context",
    "needs_human_edit",
    "missing_requested_action",
    "excellent_personalization",
)
OUTREACH_REVIEW_FEEDBACK_TAGS = (
    "too_generic",
    "too_long",
    "weak_personalization",
    "unsupported_claim_risk",
    "strong_opening",
    "good_cta",
    "good_tone",
    "worth_sending_after_light_edit",
    "not_worth_using",
)
SUGGESTED_FEEDBACK_TAGS = tuple(
    dict.fromkeys([*GENERAL_FEEDBACK_TAGS, *OUTREACH_REVIEW_FEEDBACK_TAGS])
)


def normalize_feedback_tags(value: object) -> list[str]:
    """Normalize free-form feedback tags into stable snake_case values."""

    if value is None or value == "":
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    tags: list[str] = []
    for item in values:
        tag = str(item).strip().lower().replace(" ", "_").replace("-", "_")
        if tag:
            tags.append(tag)
    return list(dict.fromkeys(tags))


def _default_quality_questions() -> list[str]:
    return [
        "Is this clear and readable for a Keystone operator?",
        "Is the tone professional and suited to Keystone Neuroinformatics?",
        "Are the personalization choices useful without adding unsupported facts?",
        "What should be approved, revised, or rejected before external use?",
    ]


class FeedbackRecord(BaseModel):
    """Human feedback on a Keystone agent output."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    object_type: FeedbackObjectType
    object_id: str = Field(min_length=1)
    approval_id: str | None = None
    source_agent: str = ""
    review_stage: FeedbackReviewStage = "approval_review"
    rating: FeedbackRating
    tags: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    notes: str = ""
    created_at: str = Field(default_factory=approval_timestamp)

    @field_validator("object_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("object_id is required.")
        return text

    @field_validator("approval_id", mode="before")
    @classmethod
    def stringify_optional_approval_id(cls, value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    @field_validator("source_agent", mode="before")
    @classmethod
    def clean_source_agent(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[str]:
        return normalize_feedback_tags(value)

    @field_validator("risk_flags", mode="before")
    @classmethod
    def normalize_risk_flags(cls, value: object) -> list[str]:
        return normalize_feedback_tags(value)

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str) -> str:
        return value.replace("\u2014", "-").strip()


class OperatorFeedbackCaptureFields(BaseModel):
    """Fixed fields an operator should record when rating an artifact."""

    model_config = ConfigDict(extra="forbid")

    object_type: str = ""
    object_id: str = ""
    rating: str = "good|okay|poor"
    tags: list[str] = Field(default_factory=list)
    notes: str = "optional short operator comments"

    @field_validator("object_type", "object_id", "rating", "notes", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("tags", mode="before")
    @classmethod
    def clean_tags(cls, value: object) -> list[str]:
        return normalize_feedback_tags(value)


class OperatorFeedbackRequest(BaseModel):
    """Optional operator prompt attached to reviewable Keystone artifacts."""

    model_config = ConfigDict(extra="forbid")

    object_type: FeedbackObjectType
    object_id: str = Field(min_length=1)
    source_agent: str = Field(min_length=1)
    review_stage: FeedbackReviewStage = "approval_review"
    approval_question: str = "Should this artifact be approved, revised, or rejected?"
    quality_questions: list[str] = Field(default_factory=_default_quality_questions)
    suggested_ratings: list[FeedbackRating] = Field(
        default_factory=lambda: ["good", "okay", "poor"]
    )
    suggested_tags: list[str] = Field(default_factory=lambda: list(SUGGESTED_FEEDBACK_TAGS))
    capture_fields: OperatorFeedbackCaptureFields = Field(
        default_factory=OperatorFeedbackCaptureFields
    )
    optional: bool = True
    send_enabled: bool = False

    @field_validator("object_id", "source_agent", "approval_question", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("quality_questions", mode="before")
    @classmethod
    def clean_questions(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("suggested_tags", mode="before")
    @classmethod
    def clean_tags(cls, value: object) -> list[str]:
        return normalize_feedback_tags(value)

    @field_validator("send_enabled")
    @classmethod
    def send_must_stay_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("operator feedback requests must not enable sending")
        return value


class FeedbackResponseResult(BaseModel):
    """Approval-linked feedback response outcome."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1)
    approval_status: str = Field(min_length=1)
    feedback: FeedbackRecord
    memory_id: int | None = None
    object_key: str = ""
    send_enabled: bool = False

    @field_validator("approval_id", "approval_status", "object_key", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("send_enabled")
    @classmethod
    def send_must_stay_disabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("feedback response results must not enable sending")
        return value
