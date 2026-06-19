"""Structured execution planning schema for Outreach Composer."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

OutreachExecutionOperation = Literal[
    "draft_follow_up",
    "draft_initial_outreach",
    "plan_reply_tracking",
    "clarification",
]


class OutreachExecutionPlan(BaseModel):
    """Agent-local plan for approval-gated outreach drafting."""

    source: str = "heuristic"
    operation: OutreachExecutionOperation = "draft_initial_outreach"
    approved_context_required: bool = True
    approved_inline_context_available: bool = False
    use_default_approved_fixture_for_backend_test: bool = False
    include_follow_up_schedule: bool = False
    include_reply_tracking_plan: bool = False
    use_example_rag: bool = False
    candidate_helpers: list[str] = Field(default_factory=list)
    side_effect_policy: str = "draft_only_never_send"
    artifact_policy: str = "draft_artifact_only_when_requested"
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "source",
        "side_effect_policy",
        "artifact_policy",
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
