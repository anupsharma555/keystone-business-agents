"""Durable WorkItem schemas for case-style agent workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


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


class WorkItemRoute(StrEnum):
    ORCHESTRATOR = "orchestrator"
    GMAIL_TRIAGE = "gmail_triage"
    BUSINESS_RESEARCH_ANALYST = "business_research_analyst"
    OPPORTUNITY_SCOUT = "opportunity_scout"
    OUTREACH_COMPOSER = "outreach_composer"
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
    supported_claim: str = ""
    provider: str = ""
    extraction_status: str = ""
    source_quality: str = ""
    retrieved_at: str = ""
    key_facts: list[str] = Field(default_factory=list)
    zotero_key: str = ""


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
    work_item_id: str | None = None
    save: bool = True
    database_url: str | None = None
    live_search: bool = False
    live_sdk: bool = False
    max_results: int = Field(default=3, ge=1, le=20)
    requested_route: WorkItemRoute | None = None
    manual_request_plan: dict[str, Any] | None = None


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
    audit_notes: list[str] = Field(default_factory=list)
    manual_request_plan: dict[str, Any] | None = None
    context_pack: dict[str, Any] | None = None
