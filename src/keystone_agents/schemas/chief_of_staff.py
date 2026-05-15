"""Structured output schema for the KNI Chief of Staff agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationInventoryReport,
    ChiefOfStaffWriteRequest,
)

ChiefOfStaffMode = Literal["deterministic", "llm", "llm_unavailable"]
ChiefOfStaffWorkflowType = Literal[
    "calendar-read",
    "gmail-summary",
    "gmail-triage",
    "business-agents-route",
    "slack-runtime-review",
    "slack-docs-review",
    "reference-capture",
    "clarification",
]


def _clean_text(value: object) -> str:
    """Return prompt-safe single-value text."""

    return str(value or "").replace("\u2014", "-").strip()


def _clean_list(value: object) -> list[str]:
    """Normalize a loose model list field into stripped strings."""

    if value is None:
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    return [_clean_text(item) for item in values if _clean_text(item)]


class ChiefOfStaffSourceRef(BaseModel):
    """One source used for an operational recommendation."""

    title: str = ""
    url: str = ""
    source_type: str = "local"
    note: str = ""

    @field_validator("title", "url", "source_type", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


class ChiefOfStaffRouteRecommendation(BaseModel):
    """A proposed Slack workflow route that still needs human approval."""

    workflow_type: ChiefOfStaffWorkflowType = "clarification"
    command_text: str = ""
    target_channel: str = ""
    rationale: str = ""
    requires_live_connector: bool = False
    requires_human_approval_before_post: bool = True

    @field_validator("command_text", "target_channel", "rationale", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("requires_human_approval_before_post")
    @classmethod
    def _approval_required(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Chief of Staff Slack routing requires approval before posting.")
        return value


class ChiefOfStaffResult(BaseModel):
    """Structured Chief of Staff recommendation with gated operating writes."""

    agent_name: str = "chief_of_staff"
    mode: ChiefOfStaffMode = "deterministic"
    intent: str = ""
    summary: str = ""
    recommended_route: ChiefOfStaffRouteRecommendation = Field(
        default_factory=ChiefOfStaffRouteRecommendation
    )
    recommended_actions: list[str] = Field(default_factory=list)
    blocked_side_effects: list[str] = Field(
        default_factory=lambda: [
            "direct_slack_post",
            "gmail_send",
            "calendar_create_or_update",
            "repo_write",
            "linkedin_publish",
            "crm_write",
        ]
    )
    approval_required: bool = True
    human_review_required: bool = True
    send_enabled: bool = False
    slack_post_allowed: bool = False
    sources: list[ChiefOfStaffSourceRef] = Field(default_factory=list)
    context_sources_considered: list[str] = Field(default_factory=list)
    repo_context_used: list[str] = Field(default_factory=list)
    automation_report: AutomationInventoryReport | None = None
    write_requests: list[ChiefOfStaffWriteRequest] = Field(default_factory=list)
    artifact_refs: list[AutomationArtifactRef] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("agent_name", "intent", "summary", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "recommended_actions",
        "blocked_side_effects",
        "context_sources_considered",
        "repo_context_used",
        "audit_notes",
        mode="before",
    )
    @classmethod
    def _clean_list_fields(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("approval_required", "human_review_required")
    @classmethod
    def _review_required(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Chief of Staff recommendations require human review and approval.")
        return value

    @field_validator("send_enabled", "slack_post_allowed")
    @classmethod
    def _side_effects_blocked(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Chief of Staff v1 cannot send or post to Slack directly.")
        return value

    @model_validator(mode="after")
    def _enforce_blocked_outputs(self) -> ChiefOfStaffResult:
        if self.recommended_route.requires_human_approval_before_post is not True:
            raise ValueError("recommended route must require human approval before posting")
        return self
