"""Small, bounded contracts for isolated KBA V2 experiments."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExperimentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=80)
    kind: Literal["first_party", "independent", "thread"]
    text: str = Field(min_length=1, max_length=3000)


class ExperimentClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=600)
    source_ids: list[str] = Field(min_length=1, max_length=6)


class ExperimentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[ExperimentClaim] = Field(default_factory=list, max_length=8)
    recommendation: str = Field(min_length=1, max_length=800)
    limitations: list[str] = Field(default_factory=list, max_length=6)
    next_step: str = Field(default="", max_length=500)
    actions_executed: Literal[False] = False


class ExperimentCriticism(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(default="", max_length=80)
    obligation_id: str = Field(default="", max_length=80)
    source_ids: list[str] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=800)
    repair: Literal["revise", "needs_source", "stop"]

    @model_validator(mode="after")
    def bind_one_target(self) -> ExperimentCriticism:
        if bool(self.claim_id) == bool(self.obligation_id):
            raise ValueError("Criticism must target exactly one claim or requested obligation.")
        if self.claim_id and not self.source_ids:
            raise ValueError("Claim criticism requires source evidence.")
        return self


class ExperimentClaimCriticism(ExperimentCriticism):
    claim_id: str = Field(
        min_length=1, max_length=80,
        description="Existing claim being criticized; obligation_id must be the empty string.",
    )
    obligation_id: Literal[""] = Field(
        default="", description="Unused for claim criticism; always the empty string.",
    )
    source_ids: list[str] = Field(
        min_length=1, max_length=6,
        description="At least one supplied source supporting criticism of this claim.",
    )


class ExperimentObligationCriticism(ExperimentCriticism):
    claim_id: Literal[""] = Field(
        default="", description="Unused for missing-work criticism; always the empty string.",
    )
    obligation_id: str = Field(
        min_length=1, max_length=80,
        description="Supplied requested obligation being criticized; claim_id must be empty.",
    )
    source_ids: list[str] = Field(
        default_factory=list, max_length=6,
        description="Supplied supporting sources, or an empty list for omitted requested work.",
    )


class ExperimentObligation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)


class ExperimentReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[ExperimentClaimCriticism | ExperimentObligationCriticism] = Field(
        default_factory=list, max_length=8,
        description="Each finding targets one claim or one requested obligation, never both.",
    )
    recommendation: Literal["accept", "revise", "needs_source", "stop"]
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def consistent_review(self) -> ExperimentReview:
        if self.recommendation == "accept" and self.findings:
            raise ValueError("An accepting review cannot contain unresolved findings.")
        if self.recommendation != "accept" and not self.findings:
            raise ValueError("A non-accepting review requires a source-bound criticism.")
        if self.recommendation == "revise" and any(f.repair != "revise" for f in self.findings):
            raise ValueError("Missing evidence or a stop finding cannot be repaired by rewriting.")
        return self


class ExperimentCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    split: Literal["development", "held_out"]
    request: str = Field(min_length=1, max_length=3000)
    sources: list[ExperimentSource] = Field(min_length=1, max_length=6)
    scripted_answer: ExperimentAnswer
    planted_error_claim_ids: list[str] = Field(default_factory=list)
    applicable_experiments: list[str] = Field(default_factory=list)
    document_fixture: Literal["", "v2_document_layout"] = ""
    obligations: list[ExperimentObligation] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded_unique_sources(self) -> ExperimentCase:
        identities = [source.source_id for source in self.sources]
        if len(identities) != len(set(identities)):
            raise ValueError("Experiment source identities must be unique.")
        if sum(len(source.text) for source in self.sources) > 12000:
            raise ValueError("Experiment source context exceeds 12000 characters.")
        obligation_ids = [item.obligation_id for item in self.obligations]
        if len(set(obligation_ids)) != len(obligation_ids):
            raise ValueError("Requested obligation identities must be unique.")
        return self


class ExperimentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: str
    title: str
    book_concept: str
    hypothesis: str
    prerequisites: list[str]
    variants: list[str]
    fault_cases: list[str]
    acceptance: list[str]
    rejection: list[str]
    later_live_question: str
    owners: list[str]


class ExperimentCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class ExperimentObservation(BaseModel):
    observation_id: str
    experiment_id: str
    case_id: str
    split: str
    variant: str
    input_fingerprint: str
    input_provenance: dict[str, Any] = Field(default_factory=dict)
    budget_limit: int | None = None
    mode: Literal["scripted_control", "fake_model", "live_model"]
    quality_status: Literal["UNMEASURED", "HUMAN_RATED"] = "UNMEASURED"
    status: Literal["completed", "blocked", "failed"] = "completed"
    checks: list[ExperimentCheck] = Field(default_factory=list)
    stages: list[dict] = Field(default_factory=list)
    answer: ExperimentAnswer | None = None
    review: ExperimentReview | None = None
    presentations: dict[str, str] = Field(default_factory=dict)
    error: str = ""
    model_requests: int = 0
    provider_writes: Literal[0] = 0
    elapsed_ms: float = 0


class ExperimentReport(BaseModel):
    schema_name: Literal["keystone.v2_experiments.v1"] = "keystone.v2_experiments.v1"
    run_id: str
    created_at: str
    mode: str
    definitions: list[ExperimentDefinition]
    observations: list[ExperimentObservation]
    model_requests: int = 0
    budget_limit: int | None = None
    requested_budget_limit: int | None = None
    provider_writes: Literal[0] = 0
    quality_status: Literal["UNMEASURED", "HUMAN_RATED"] = "UNMEASURED"
    human_ratings: dict[str, dict] = Field(default_factory=dict)
    runtime: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
