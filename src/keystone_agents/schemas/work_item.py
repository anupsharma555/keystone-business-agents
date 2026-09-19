"""Durable WorkItem schemas for case-style agent workflows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from keystone_agents.schemas.signal_lifecycle import SignalTriggerContext
from keystone_agents.schemas.source_evidence import (
    SourceEvidenceAccess,
    SourceEvidencePage,
    source_evidence_access,
)
from keystone_agents.schemas.web_source import WebSourceAccess


def utc_now_iso() -> str:
    """Return a stable UTC timestamp for WorkItem records."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_work_item_id() -> str:
    """Return an opaque WorkItem id."""

    return f"wi_{uuid4().hex}"


class WorkItemKind(StrEnum):
    GMAIL_THREAD = "gmail_thread"
    COMPANY_RESEARCH = "company_research"
    RESEARCH_BRIEF = "research_brief"
    RAG_RETRIEVAL = "rag_retrieval"
    OPPORTUNITY = "opportunity"
    OUTREACH = "outreach"
    WEEKLY_SCAN = "weekly_scan"


class WorkItemStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    NEEDS_CONTEXT = "needs_context"
    NEEDS_APPROVAL = "needs_approval"
    BLOCKED = "blocked"
    DONE = "done"
    ARCHIVED = "archived"


class UserFacingSummaryAuthority(StrEnum):
    """Whether final review may recast the current operator-facing summary."""

    REVIEWABLE = "reviewable"
    CANONICAL = "canonical"


class WorkItemRoute(StrEnum):
    ORCHESTRATOR = "orchestrator"
    GMAIL_TRIAGE = "gmail_triage"
    BUSINESS_RESEARCH_ANALYST = "business_research_analyst"
    RAG_RETRIEVAL_SPECIALIST = "rag_retrieval_specialist"
    OPPORTUNITY_SCOUT = "opportunity_scout"
    OUTREACH_COMPOSER = "outreach_composer"
    CHIEF_OF_STAFF = "chief_of_staff"
    RSS_CONTEXT_AGENT = "rss_context_agent"
    PREPRINTS_CONTEXT_AGENT = "preprints_context_agent"
    CLARIFICATION = "clarification"


class WorkItemTarget(BaseModel):
    """The main target entity or topic for a WorkItem."""

    name: str = ""
    url: str = ""
    email: str = ""
    object_type: str = ""
    external_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkItemSourceRef(BaseModel):
    """Compact reference to a source supporting a WorkItem."""

    title: str = ""
    url: str = ""
    source_type: str = ""
    source_id: str = ""
    provider_candidate_id: str = Field(default="", max_length=200)
    supported_claim: str = ""
    provider: str = ""
    extraction_status: str = ""
    source_quality: str = ""
    retrieved_at: str = ""
    key_facts: list[str] = Field(default_factory=list)
    evidence_excerpt: str = ""
    zotero_key: str = ""
    evidence_pages: list[SourceEvidencePage] = Field(default_factory=list)
    evidence_access: SourceEvidenceAccess | None = None
    web_source_access: WebSourceAccess | None = None

    @model_validator(mode="after")
    def bind_evidence_snapshot(self) -> WorkItemSourceRef:
        if self.evidence_pages:
            actual = source_evidence_access(self.evidence_pages)
            if self.evidence_access is not None and self.evidence_access != actual:
                raise ValueError("Retained source pages do not match their evidence snapshot.")
            self.evidence_access = actual
        return self

    def model_context(self) -> WorkItemSourceRef:
        """Keep an exact read handle while excluding retained pages from the prompt."""
        return self.model_copy(update={"evidence_pages": []})


class WorkItemFact(BaseModel):
    """Small approved fact attached to a WorkItem."""

    key: str
    value: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_refs: list[WorkItemSourceRef] = Field(default_factory=list)
    approval_state: str = "pending"


class WorkItemArtifactRef(BaseModel):
    """Reference to a stored artifact row without duplicating its payload."""

    artifact_type: str
    artifact_id: str
    source_agent: str = ""
    approval_state: str = "pending"
    title: str = ""
    summary: str = ""
    selected: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("artifact_id", mode="before")
    @classmethod
    def _stringify_artifact_id(cls, value: object) -> str:
        return str(value or "")


class WorkItemApprovalGate(BaseModel):
    """Approval state needed before a future step can continue."""

    scope: str
    state: str = "pending"
    required: bool = True
    rationale: str = ""
    approval_id: str = ""


class WorkItemBlocker(BaseModel):
    """Deterministic reason a WorkItem cannot advance."""

    code: str
    message: str
    severity: str = "blocker"
    resolved: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    resolved_at: str = ""


class WorkItemNextAction(BaseModel):
    """The next safe deterministic action for a WorkItem."""

    action: str
    agent: WorkItemRoute | None = None
    description: str = ""
    command_hint: str = ""
    requires_approval: bool = False


class WorkItemEvent(BaseModel):
    """Append-only event for WorkItem audit history."""

    event_type: str
    actor: str = "system"
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class WorkItem(BaseModel):
    """Durable workflow object that wraps specialist artifacts and state."""

    id: str = Field(default_factory=new_work_item_id)
    kind: WorkItemKind
    status: WorkItemStatus = WorkItemStatus.NEW
    title: str
    request_text: str = ""
    target: WorkItemTarget = Field(default_factory=WorkItemTarget)
    signal_trigger: SignalTriggerContext | None = None
    current_route: WorkItemRoute = WorkItemRoute.ORCHESTRATOR
    facts: list[WorkItemFact] = Field(default_factory=list)
    sources: list[WorkItemSourceRef] = Field(default_factory=list)
    artifact_refs: list[WorkItemArtifactRef] = Field(default_factory=list)
    approval_gates: list[WorkItemApprovalGate] = Field(default_factory=list)
    blockers: list[WorkItemBlocker] = Field(default_factory=list)
    next_action: WorkItemNextAction | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    last_agent: str = ""
    audit_notes: list[str] = Field(default_factory=list)

    def touch(self) -> WorkItem:
        """Return a copy with updated timestamp."""

        return self.model_copy(update={"updated_at": utc_now_iso()})


class WorkflowRunRequest(BaseModel):
    """Input for one deterministic WorkItem advancement."""

    request_text: str = ""
    execution_id: str = ""
    origin_event_id: str = ""
    model_request_limit: int | None = Field(default=None, ge=0)
    work_item_id: str | None = None
    save: bool = True
    database_url: str | None = None
    live_search: bool = False
    live_sdk: bool = False
    live_rss_slack_read: bool = False
    max_results: int = Field(default=3, ge=1, le=20)
    max_results_explicit: bool = Field(
        default=False,
        description=(
            "Whether max_results is an explicit caller ceiling rather than a fallback default."
        ),
    )
    requested_route: WorkItemRoute | None = None
    manual_request_plan: dict[str, Any] | None = None
    orchestrator_preflight: dict[str, Any] | None = None
    context_file_path: str = ""
    context_file_snapshot: dict[str, Any] | None = Field(default=None, repr=False)
    external_context: dict[str, Any] | None = None
    slack_query_prompt: dict[str, Any] | None = None
    sdk_session_enabled: bool | None = None
    sdk_session_id: str = ""
    sdk_session_db_path: str = ""
    sdk_session_history_limit: int | None = Field(default=None, ge=1)
    cost_profile: str = "standard"
    allow_manager_loop_repair: bool = True
    include_contact_enrichment: bool = True
    hosted_web_search_max_calls: int | None = Field(default=None, ge=0, le=20)
    reuse_existing_research: bool = False
    cost_tracking_requested: bool = False

    @model_validator(mode="before")
    @classmethod
    def capture_result_limit_origin(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            payload = dict(value)
            payload.setdefault("max_results_explicit", "max_results" in payload)
            return payload
        return value

    @classmethod
    def from_checkpoint(cls, value: Any) -> WorkflowRunRequest:
        """Read persisted provenance; legacy snapshots never establish an explicit cap."""
        if isinstance(value, Mapping):
            value = dict(value)
            value.setdefault("max_results_explicit", False)
        return cls.model_validate(value)


class WorkflowExecutionProvenance(BaseModel):
    """Resolved model and retrieval mode for one WorkItem advancement."""

    run_mode: str = "fixture"
    live_sdk: bool = False
    live_search: bool = False
    model_provider: str = ""
    model_name: str = ""
    model_source: str = "none"
    search_provider: str = ""
    search_provider_sequence: list[str] = Field(default_factory=list)
    search_source: str = "none"


class WorkflowExecutionStep(BaseModel):
    """One bounded, privacy-safe WorkItem execution event for eval tracing."""

    step_index: int = Field(ge=1)
    category: str
    name: str
    status: str = "observed"
    duration_ms: float | None = Field(default=None, ge=0)
    error_kind: str = ""
    provider: str = ""
    request_count: int = Field(default=0, ge=0)
    source_count: int = Field(default=0, ge=0)
    visible_source_count: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0, le=1)
    approval_required: bool = False
    blocker_count: int = Field(default=0, ge=0)


class WorkflowRunResult(BaseModel):
    """Result of one WorkItem advancement."""

    work_item: WorkItem
    route: WorkItemRoute
    status: WorkItemStatus
    advanced: bool
    artifact_refs: list[WorkItemArtifactRef] = Field(default_factory=list)
    blockers: list[WorkItemBlocker] = Field(default_factory=list)
    next_action: WorkItemNextAction | None = None
    human_summary: str = ""
    user_facing_summary_authority: UserFacingSummaryAuthority = (
        UserFacingSummaryAuthority.REVIEWABLE
    )
    audit_notes: list[str] = Field(default_factory=list)
    manual_request_plan: dict[str, Any] | None = None
    orchestrator_preflight: dict[str, Any] | None = None
    context_pack: dict[str, Any] | None = None
    nested_specialist_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_provenance: WorkflowExecutionProvenance = Field(
        default_factory=WorkflowExecutionProvenance
    )
    execution_steps: list[WorkflowExecutionStep] = Field(default_factory=list)
    tool_execution: dict[str, Any] = Field(default_factory=dict)
