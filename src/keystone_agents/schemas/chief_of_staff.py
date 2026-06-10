"""Structured output schema for the KNI Chief of Staff agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationInventoryReport,
    ChiefOfStaffWriteRequest,
)
from keystone_agents.schemas.memory import ChiefOfStaffMemoryContext

ChiefOfStaffMode = Literal["deterministic", "llm", "llm_unavailable"]
ChiefOfStaffSlackPostPolicy = Literal[
    "not_allowed",
    "draft_only",
    "channel_policy_allowed",
    "requires_human_review",
]
ChiefOfStaffWorkflowType = Literal[
    "calendar-read",
    "gmail-summary",
    "gmail-triage",
    "business-agents-route",
    "slack-runtime-review",
    "slack-cross-channel-review",
    "slack-article-review",
    "slack-follow-up-review",
    "slack-docs-review",
    "project-context-review",
    "research-direction-review",
    "budget-resource-review",
    "meeting-prep",
    "portfolio-review",
    "artifact-write-plan",
    "google-drive-management",
    "reference-capture",
    "memory-review",
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


class ChiefOfStaffResult(BaseModel):
    """Structured Chief of Staff recommendation with gated operating writes."""

    agent_name: str = "chief_of_staff"
    mode: ChiefOfStaffMode = "deterministic"
    intent: str = ""
    summary: str = ""
    synthesis: str = ""
    time_window: str = ""
    target_channels: list[str] = Field(default_factory=list)
    operating_capabilities: list[str] = Field(default_factory=list)
    recommended_route: ChiefOfStaffRouteRecommendation = Field(
        default_factory=ChiefOfStaffRouteRecommendation
    )
    recommended_actions: list[str] = Field(default_factory=list)
    blocked_side_effects: list[str] = Field(
        default_factory=lambda: [
            "unscoped_slack_post",
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
    slack_post_policy: ChiefOfStaffSlackPostPolicy = "not_allowed"
    slack_target_channel: str = ""
    slack_post_reason: str = ""
    sources: list[ChiefOfStaffSourceRef] = Field(default_factory=list)
    context_sources_considered: list[str] = Field(default_factory=list)
    repo_context_used: list[str] = Field(default_factory=list)
    automation_report: AutomationInventoryReport | None = None
    memory_context: ChiefOfStaffMemoryContext | None = None
    write_requests: list[ChiefOfStaffWriteRequest] = Field(default_factory=list)
    artifact_refs: list[AutomationArtifactRef] = Field(default_factory=list)
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("agent_name", "intent", "summary", "synthesis", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "recommended_actions",
        "blocked_side_effects",
        "context_sources_considered",
        "repo_context_used",
        "audit_notes",
        "target_channels",
        "operating_capabilities",
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

    @field_validator("send_enabled")
    @classmethod
    def _email_send_blocked(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Chief of Staff cannot send email or external messages directly.")
        return value

    @field_validator(
        "slack_post_policy",
        "slack_target_channel",
        "slack_post_reason",
        mode="before",
    )
    @classmethod
    def _clean_slack_policy_fields(cls, value: object) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _enforce_blocked_outputs(self) -> ChiefOfStaffResult:
        route_channel = self.recommended_route.target_channel.strip()
        target_channel = self.slack_target_channel.strip() or route_channel
        if self.slack_post_allowed:
            if self.slack_post_policy != "channel_policy_allowed":
                raise ValueError(
                    "slack_post_allowed requires slack_post_policy='channel_policy_allowed'."
                )
            if not target_channel:
                raise ValueError("slack_post_allowed requires a target Slack channel.")
            if "unscoped_slack_post" in self.blocked_side_effects:
                raise ValueError(
                    "slack_post_allowed cannot leave unscoped_slack_post in blocked_side_effects."
                )
        elif self.slack_post_policy == "channel_policy_allowed":
            raise ValueError("channel_policy_allowed requires slack_post_allowed=True.")

        if (
            self.recommended_route.requires_human_approval_before_post is not True
            and not self.slack_post_allowed
        ):
            raise ValueError(
                "Slack routing can skip human approval only for channel-policy-allowed posts."
            )
        return self
