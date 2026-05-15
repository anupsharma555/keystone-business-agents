"""Structured planning schema for manual Keystone agent requests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ManualTargetAgent = Literal[
    "orchestrator",
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "chief_of_staff",
    "clarification",
]

ManualRequestIntent = Literal[
    "route_request",
    "company_research",
    "research_brief",
    "opportunity_search",
    "opportunity_to_outreach_loop",
    "gmail_triage",
    "outreach_draft",
    "slack_operations",
    "reference_capture",
    "continue_work_item",
    "blocked_send",
    "clarification",
]

ManualTargetType = Literal[
    "company",
    "person",
    "institute",
    "conference",
    "zotero_collection",
    "zotero_article",
    "article_collection",
    "gmail_thread",
    "topic",
    "opportunity",
    "operator_reference",
    "slack_channel",
    "unknown",
]


class ManualRequestPlan(BaseModel):
    """Pre-execution semantic plan for manual CLI and Slack agent calls."""

    source: str = "heuristic"
    requested_agent: ManualTargetAgent | None = None
    target_agent: ManualTargetAgent = "clarification"
    intent: ManualRequestIntent = "clarification"
    primary_target: str = ""
    target_type: ManualTargetType = "unknown"
    objective: str = ""
    desired_count: int = Field(default=1, ge=1, le=10)
    constraints: list[str] = Field(default_factory=list)
    gmail_query: str = ""
    lookback_days: int | None = None
    draft_policy: str = ""
    recipient: str = ""
    outreach_channel: str = ""
    tone: str = ""
    requires_live_search: bool = False
    requires_approved_context: bool = False
    side_effect_policy: str = "draft_or_read_only"
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "source",
        "primary_target",
        "objective",
        "gmail_query",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
        "side_effect_policy",
        "rationale",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("lookback_days", mode="before")
    @classmethod
    def _clean_lookback_days(cls, value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return None

    @field_validator("constraints", "planner_warnings", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("desired_count", mode="before")
    @classmethod
    def _clean_desired_count(cls, value: object) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 1
