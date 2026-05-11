"""Typed WorkItem-derived context packs for specialist agents."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from keystone_agents.schemas.work_item import (
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemFact,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)


class ContextPackReadinessGate(BaseModel):
    """One deterministic gate applied before a specialist receives context."""

    name: str
    ready: bool
    required: bool = True
    blockers: list[WorkItemBlocker] = Field(default_factory=list)
    next_action: WorkItemNextAction | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ContextPackBase(BaseModel):
    """Common WorkItem state shared by every specialist context pack."""

    pack_type: str
    work_item_id: str
    route: WorkItemRoute
    current_status: WorkItemStatus
    target: WorkItemTarget = Field(default_factory=WorkItemTarget)
    request_text: str = ""
    approved_facts: list[WorkItemFact] = Field(default_factory=list)
    source_refs: list[WorkItemSourceRef] = Field(default_factory=list)
    retrieved_sources: list[WorkItemSourceRef] = Field(default_factory=list)
    selected_artifacts: list[WorkItemArtifactRef] = Field(default_factory=list)
    blockers: list[WorkItemBlocker] = Field(default_factory=list)
    approval_gates: list[WorkItemApprovalGate] = Field(default_factory=list)
    allowed_next_action: WorkItemNextAction | None = None
    readiness_gates: list[ContextPackReadinessGate] = Field(default_factory=list)
    ready: bool = False
    can_synthesize: bool = False
    missing_requirements: list[str] = Field(default_factory=list)
    limitation_notes: list[str] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ResearchContextPack(ContextPackBase):
    """Context contract for company or account research."""

    pack_type: Literal["research"] = "research"
    route: WorkItemRoute = WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    research_goal: str = ""
    source_bundle_summary: str = ""
    missing_evidence: list[str] = Field(default_factory=list)


class OpportunityContextPack(ContextPackBase):
    """Context contract for opportunity scouting."""

    pack_type: Literal["opportunity"] = "opportunity"
    route: WorkItemRoute = WorkItemRoute.OPPORTUNITY_SCOUT
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    retrieved_candidates: list[WorkItemArtifactRef] = Field(default_factory=list)
    review_candidates: list[WorkItemArtifactRef] = Field(default_factory=list)
    source_sufficiency: str = "unknown"
    approval_state: str = "pending"


class OutreachContactContext(BaseModel):
    """Recipient and channel context available for outreach drafting."""

    name: str = ""
    organization: str = ""
    email: str = ""
    linkedin_url: str = ""
    channel: str = ""
    source: str = ""
    source_ids: list[str] = Field(default_factory=list)
    readiness_level: str = "missing"
    missing: list[str] = Field(default_factory=list)


class OutreachContextPack(ContextPackBase):
    """Context contract for draft-only outbound outreach."""

    pack_type: Literal["outreach"] = "outreach"
    route: WorkItemRoute = WorkItemRoute.OUTREACH_COMPOSER
    selected_company_artifact: WorkItemArtifactRef | None = None
    selected_opportunity_artifact: WorkItemArtifactRef | None = None
    contact: OutreachContactContext = Field(default_factory=OutreachContactContext)
    channel: str = ""
    allowed_claims: list[WorkItemFact] = Field(default_factory=list)
    blocked_claims: list[WorkItemFact] = Field(default_factory=list)
    approval_state: str = "pending"


class GmailContextPack(ContextPackBase):
    """Context contract for Gmail triage or draft-only reply work."""

    pack_type: Literal["gmail"] = "gmail"
    route: WorkItemRoute = WorkItemRoute.GMAIL_TRIAGE
    thread_id: str = ""
    message_id: str = ""
    selected_thread_ids: list[str] = Field(default_factory=list)
    selected_message_ids: list[str] = Field(default_factory=list)
    thread_summary: str = ""
    prior_reply_context: str = ""
    sender: str = ""
    reply_objective: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    approval_state: str = "pending"


ContextPack: TypeAlias = (
    ResearchContextPack | OpportunityContextPack | OutreachContextPack | GmailContextPack
)
