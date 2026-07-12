"""Compact schemas for bounded live manager route/correction validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ManagerOwner = Literal[
    "chief_of_staff",
    "business_research_analyst",
    "opportunity_scout",
    "gmail_triage",
    "outreach_composer",
    "clarification",
]


class AdvancedManagerRouteDecision(BaseModel):
    """One compact manager decision over the current turn and session context."""

    selected_owner: ManagerOwner
    rejected_owners: list[ManagerOwner] = Field(default_factory=list)
    current_objective: str = Field(min_length=1)
    operation_boundary: Literal["read_only", "draft_only", "approval_checkpoint"]
    latest_instruction_applied: bool
    prior_direction_considered: bool = False
    side_effects_allowed: bool = False
    clarification_needed: bool = False
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_safety_and_route(self) -> AdvancedManagerRouteDecision:
        if self.side_effects_allowed:
            raise ValueError("Manager route validation cannot allow side effects.")
        if self.selected_owner in self.rejected_owners:
            raise ValueError("Selected owner cannot also be rejected.")
        if self.selected_owner == "clarification" and not self.clarification_needed:
            raise ValueError("Clarification owner requires clarification_needed=true.")
        return self
