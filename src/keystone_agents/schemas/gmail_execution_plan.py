"""Structured execution planning schema for Gmail Triage."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

GmailExecutionOperation = Literal[
    "priority_grouping",
    "thread_summary",
    "single_message_triage",
    "draft_reply",
    "update_draft",
    "style_profile",
    "clarification",
]


class GmailExecutionPlan(BaseModel):
    """Agent-local plan for Gmail read, triage, and draft-only workflows."""

    source: str = "heuristic"
    operation: GmailExecutionOperation = "single_message_triage"
    lookback_days: int = Field(default=3, ge=1, le=365)
    max_messages: int = Field(default=10, ge=1, le=50)
    gmail_query: str = ""
    source_label: str = "INBOX"
    draft_subject_hint: str = ""
    draft_recipient_hint: str = ""
    create_gmail_drafts: bool = False
    draft_replies_in_output: bool = False
    live_read_required: bool = False
    candidate_helpers: list[str] = Field(default_factory=list)
    artifact_policy: str = "no_artifact_unless_requested"
    side_effect_policy: str = "read_only_or_draft_only"
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "source",
        "gmail_query",
        "source_label",
        "draft_subject_hint",
        "draft_recipient_hint",
        "artifact_policy",
        "side_effect_policy",
        "rationale",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("candidate_helpers", "planner_warnings", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]
