"""Typed contracts for model-owned semantic decisions and Python validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DecisionOwner = Literal[
    "specialist_agent",
    "chief_of_staff",
    "orchestrator",
    "operator",
]
DecisionValidatorStatus = Literal[
    "not_evaluated",
    "accepted",
    "repair_required",
    "rejected",
]
DecisionTelemetryEventType = Literal[
    "proposed",
    "validator_result",
    "repair_proposed",
    "terminal",
]


class DecisionCandidateAssessment(BaseModel):
    """One candidate the model considered without exposing provider-private content."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=200)
    disposition: Literal["selected", "excluded", "plausible", "needs_more_context"]
    rationale: str = Field(min_length=1, max_length=1_000)

    @field_validator("candidate_id", "rationale", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").replace("\u2014", "-").split())


class AgentDecisionRecord(BaseModel):
    """Semantic choice made by an agent over a bounded candidate set."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.agent_decision.v1"] = "keystone.agent_decision.v1"
    decision_owner: DecisionOwner = "specialist_agent"
    decision_stage: str = Field(default="specialist_selection", min_length=1, max_length=120)
    selected_candidate_id: str = Field(
        default="", max_length=200,
        description=(
            "Optional one chosen identity, not a separate alternative. Together with "
            "selected_candidate_ids it defines the selected set. When selected assessments "
            "are supplied, that set must exactly equal their candidate IDs."
        ),
    )
    selected_candidate_ids: list[str] = Field(
        default_factory=list, max_length=20,
        description=(
            "Chosen identities, not merely plausible or excluded alternatives. Together "
            "with a nonempty selected_candidate_id, these must exactly match the IDs "
            "assessed as selected when such assessments are supplied. Prefer listing "
            "the complete selected set here, including the scalar identity if used."
        ),
    )
    candidate_assessments: list[DecisionCandidateAssessment] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Assess each identity at most once. All chosen identities must be marked "
            "selected; plausible and excluded are for nonselected alternatives. IDs "
            "marked selected must match the scalar/list union when explicit selections "
            "are populated. Assessment order does not determine execution order."
        ),
    )
    reasoning: str = Field(default="", max_length=2_000)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    needs_more_context: bool = False

    @field_validator("decision_stage", "selected_candidate_id", "reasoning", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").replace("\u2014", "-").split())

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_limitations(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                " ".join(str(item or "").replace("\u2014", "-").split())
                for item in values
                if str(item or "").strip()
            )
        )[:12]

    @field_validator("selected_candidate_ids", mode="before")
    @classmethod
    def _clean_selected_ids(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                " ".join(str(item or "").split())[:200]
                for item in values
                if str(item or "").strip()
            )
        )[:20]

    @model_validator(mode="after")
    def _validate_selection_shape(self) -> AgentDecisionRecord:
        assessed_ids = [item.candidate_id for item in self.candidate_assessments]
        if len(assessed_ids) != len(set(assessed_ids)):
            raise ValueError("A decision cannot assess the same candidate twice.")
        selected = [
            item.candidate_id
            for item in self.candidate_assessments
            if item.disposition == "selected"
        ]
        explicit_selected = list(self.selected_candidate_ids)
        if self.selected_candidate_id and self.selected_candidate_id not in explicit_selected:
            explicit_selected.insert(0, self.selected_candidate_id)
        if selected and explicit_selected and set(selected) != set(explicit_selected):
            raise ValueError("Selected identity fields must match selected assessments.")
        assessed_dispositions = {
            item.candidate_id: item.disposition for item in self.candidate_assessments
        }
        contradictory = [
            candidate_id
            for candidate_id in explicit_selected
            if candidate_id in assessed_dispositions
            and assessed_dispositions[candidate_id] != "selected"
        ]
        if contradictory:
            raise ValueError(
                "Every assessed selected identity must have disposition='selected'."
            )
        self.selected_candidate_ids = list(dict.fromkeys(explicit_selected or selected))
        if not self.selected_candidate_id and len(self.selected_candidate_ids) == 1:
            self.selected_candidate_id = self.selected_candidate_ids[0]
        return self


class DecisionValidatorOutcome(BaseModel):
    """Deterministic validation of an agent choice without semantic substitution."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.decision_validator.v1"] = (
        "keystone.decision_validator.v1"
    )
    status: DecisionValidatorStatus = "not_evaluated"
    decision_stage: str = Field(default="specialist_selection", min_length=1, max_length=120)
    selected_candidate_id: str = Field(default="", max_length=200)
    candidate_count: int = Field(default=0, ge=0, le=100)
    selected_identity_in_candidate_set: bool | None = None
    selected_identity_was_read: bool | None = None
    reason_code: str = Field(default="", max_length=160)
    feedback: str = Field(default="", max_length=1_000)
    repair_attempted: bool = False

    @field_validator(
        "decision_stage",
        "selected_candidate_id",
        "reason_code",
        "feedback",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").replace("\u2014", "-").split())


class DecisionTelemetryEvent(BaseModel):
    """Trace-safe lifecycle event for one semantic decision attempt."""

    model_config = ConfigDict(extra="forbid")

    event_type: DecisionTelemetryEventType
    decision_owner: DecisionOwner
    decision_stage: str = Field(min_length=1, max_length=120)
    attempt: int = Field(default=1, ge=1, le=3)
    candidate_ids: list[str] = Field(default_factory=list, max_length=100)
    selected_candidate_ids: list[str] = Field(default_factory=list, max_length=20)
    excluded_candidate_ids: list[str] = Field(default_factory=list, max_length=100)
    validator_status: DecisionValidatorStatus = "not_evaluated"
    reason_code: str = Field(default="", max_length=160)
    reasoning: str = Field(default="", max_length=2_000)
    limitations: list[str] = Field(default_factory=list, max_length=12)
    tool_mode: Literal[
        "model_called",
        "workflow_called",
        "preacquired_verified_context",
        "verified_context_tool_free",
        "tool_free",
    ] = "tool_free"

    @field_validator("decision_stage", "reason_code", "reasoning", mode="before")
    @classmethod
    def _clean_event_text(cls, value: object) -> str:
        return " ".join(str(value or "").replace("\u2014", "-").split())

    @field_validator(
        "candidate_ids",
        "selected_candidate_ids",
        "excluded_candidate_ids",
        mode="before",
    )
    @classmethod
    def _clean_event_ids(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                " ".join(str(item or "").split())[:200]
                for item in values
                if str(item or "").strip()
            )
        )

    @field_validator("limitations", mode="before")
    @classmethod
    def _clean_event_limitations(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                " ".join(str(item or "").replace("\u2014", "-").split())
                for item in values
                if str(item or "").strip()
            )
        )[:12]


__all__ = [
    "AgentDecisionRecord",
    "DecisionCandidateAssessment",
    "DecisionTelemetryEvent",
    "DecisionTelemetryEventType",
    "DecisionOwner",
    "DecisionValidatorOutcome",
    "DecisionValidatorStatus",
]
