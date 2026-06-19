"""Typed WorkItem-derived context packs for specialist agents."""

from __future__ import annotations

from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field

from keystone_agents.schemas.handoff_types import HandoffTypeContract, build_handoff_type_contract
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


class MemoryContextRef(BaseModel):
    """Prompt-safe reference to approved durable memory."""

    id: int | None = None
    memory_type: str
    object_type: str = ""
    object_id: str = ""
    object_key: str = ""
    title: str = ""
    summary: str = ""
    content_summary: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    approval_state: str = ""
    confidence: float = 0.0
    created_at: str = ""


class ContextPackBase(BaseModel):
    """Common WorkItem state shared by every specialist context pack."""

    pack_type: str
    handoff_type_contract: HandoffTypeContract = Field(default_factory=HandoffTypeContract)
    input_type: str = "keystone_agents.schemas.work_item.WorkItem"
    satisfies_input_type: str = ""
    expected_output_type: str = ""
    next_input_type: str = ""
    type_compatibility_status: str = "unknown"
    work_item_id: str
    route: WorkItemRoute
    current_status: WorkItemStatus
    target: WorkItemTarget = Field(default_factory=WorkItemTarget)
    request_text: str = ""
    approved_facts: list[WorkItemFact] = Field(default_factory=list)
    source_refs: list[WorkItemSourceRef] = Field(default_factory=list)
    retrieved_sources: list[WorkItemSourceRef] = Field(default_factory=list)
    source_context_status: dict[str, Any] = Field(default_factory=dict)
    source_context_sample: list[dict[str, Any]] = Field(default_factory=list)
    source_context_focus: dict[str, Any] = Field(default_factory=dict)
    source_triage: dict[str, Any] = Field(default_factory=dict)
    ordered_sources: list[dict[str, Any]] = Field(default_factory=list)
    selected_artifacts: list[WorkItemArtifactRef] = Field(default_factory=list)
    blockers: list[WorkItemBlocker] = Field(default_factory=list)
    approval_gates: list[WorkItemApprovalGate] = Field(default_factory=list)
    allowed_next_action: WorkItemNextAction | None = None
    readiness_gates: list[ContextPackReadinessGate] = Field(default_factory=list)
    ready: bool = False
    can_synthesize: bool = False
    missing_requirements: list[str] = Field(default_factory=list)
    limitation_notes: list[str] = Field(default_factory=list)
    relevant_memory_refs: list[MemoryContextRef] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class ResearchContextPack(ContextPackBase):
    """Context contract for company or account research."""

    pack_type: Literal["research"] = "research"
    handoff_type_contract: HandoffTypeContract = Field(
        default_factory=lambda: build_handoff_type_contract(
            source_agent="work_item_manager",
            target_agent="business_research_analyst",
            source_output_type="keystone_agents.schemas.work_item.WorkItem",
            target_input_type="keystone_agents.schemas.context_pack.ResearchContextPack",
            target_output_type="keystone_agents.schemas.research.ResearchBrief",
            payload_mode="adapted",
            parsed_output_status="parsed",
            compatibility_notes=[
                "WorkItem state has been adapted into a ResearchContextPack before specialist execution."
            ],
        )
    )
    satisfies_input_type: str = "keystone_agents.schemas.context_pack.ResearchContextPack"
    expected_output_type: str = "keystone_agents.schemas.research.ResearchBrief"
    next_input_type: str = "keystone_agents.schemas.context_pack.OutreachContextPack"
    type_compatibility_status: str = "compatible"
    route: WorkItemRoute = WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    research_goal: str = ""
    source_bundle_summary: str = ""
    missing_evidence: list[str] = Field(default_factory=list)
    approved_company_facts: list[MemoryContextRef] = Field(default_factory=list)
    retrieval_performance_notes: list[MemoryContextRef] = Field(default_factory=list)


class OpportunityContextPack(ContextPackBase):
    """Context contract for opportunity scouting."""

    pack_type: Literal["opportunity"] = "opportunity"
    handoff_type_contract: HandoffTypeContract = Field(
        default_factory=lambda: build_handoff_type_contract(
            source_agent="work_item_manager",
            target_agent="opportunity_scout",
            source_output_type="keystone_agents.schemas.work_item.WorkItem",
            target_input_type="keystone_agents.schemas.context_pack.OpportunityContextPack",
            target_output_type="keystone_agents.schemas.opportunity.OpportunityScoutResult",
            payload_mode="adapted",
            parsed_output_status="parsed",
            compatibility_notes=[
                "WorkItem state has been adapted into an OpportunityContextPack before specialist execution."
            ],
        )
    )
    satisfies_input_type: str = "keystone_agents.schemas.context_pack.OpportunityContextPack"
    expected_output_type: str = "keystone_agents.schemas.opportunity.OpportunityScoutResult"
    next_input_type: str = "keystone_agents.schemas.context_pack.ResearchContextPack"
    type_compatibility_status: str = "compatible"
    route: WorkItemRoute = WorkItemRoute.OPPORTUNITY_SCOUT
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    retrieved_candidates: list[WorkItemArtifactRef] = Field(default_factory=list)
    review_candidates: list[WorkItemArtifactRef] = Field(default_factory=list)
    source_sufficiency: str = "unknown"
    approval_state: str = "pending"
    prior_opportunity_refs: list[MemoryContextRef] = Field(default_factory=list)
    retrieval_performance_notes: list[MemoryContextRef] = Field(default_factory=list)


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
    handoff_type_contract: HandoffTypeContract = Field(
        default_factory=lambda: build_handoff_type_contract(
            source_agent="work_item_manager",
            target_agent="outreach_composer",
            source_output_type="keystone_agents.schemas.work_item.WorkItem",
            target_input_type="keystone_agents.schemas.context_pack.OutreachContextPack",
            target_output_type="keystone_agents.schemas.outreach.OutreachDraft",
            payload_mode="adapted",
            parsed_output_status="parsed",
            compatibility_notes=[
                "WorkItem state has been adapted into an OutreachContextPack before draft-only execution."
            ],
        )
    )
    satisfies_input_type: str = "keystone_agents.schemas.context_pack.OutreachContextPack"
    expected_output_type: str = "keystone_agents.schemas.outreach.OutreachDraft"
    next_input_type: str = "keystone_agents.schemas.orchestrator.OrchestratorResult"
    type_compatibility_status: str = "compatible"
    route: WorkItemRoute = WorkItemRoute.OUTREACH_COMPOSER
    selected_company_artifact: WorkItemArtifactRef | None = None
    selected_opportunity_artifact: WorkItemArtifactRef | None = None
    contact: OutreachContactContext = Field(default_factory=OutreachContactContext)
    channel: str = ""
    allowed_claims: list[WorkItemFact] = Field(default_factory=list)
    blocked_claims: list[WorkItemFact] = Field(default_factory=list)
    approval_state: str = "pending"
    approved_company_facts: list[MemoryContextRef] = Field(default_factory=list)
    prior_opportunity_refs: list[MemoryContextRef] = Field(default_factory=list)
    outreach_style_examples: list[MemoryContextRef] = Field(default_factory=list)
    approval_history_refs: list[MemoryContextRef] = Field(default_factory=list)


class GmailContextPack(ContextPackBase):
    """Context contract for Gmail triage or draft-only reply work."""

    pack_type: Literal["gmail"] = "gmail"
    handoff_type_contract: HandoffTypeContract = Field(
        default_factory=lambda: build_handoff_type_contract(
            source_agent="work_item_manager",
            target_agent="gmail_triage",
            source_output_type="keystone_agents.schemas.work_item.WorkItem",
            target_input_type="keystone_agents.schemas.context_pack.GmailContextPack",
            target_output_type="keystone_agents.schemas.email_triage.EmailTriageResult",
            payload_mode="adapted",
            parsed_output_status="parsed",
            compatibility_notes=[
                "WorkItem state has been adapted into a GmailContextPack before triage execution."
            ],
        )
    )
    satisfies_input_type: str = "keystone_agents.schemas.context_pack.GmailContextPack"
    expected_output_type: str = "keystone_agents.schemas.email_triage.EmailTriageResult"
    next_input_type: str = "keystone_agents.schemas.context_pack.ResearchContextPack"
    type_compatibility_status: str = "compatible"
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
    outreach_style_examples: list[MemoryContextRef] = Field(default_factory=list)
    approval_history_refs: list[MemoryContextRef] = Field(default_factory=list)


ContextPack: TypeAlias = (
    ResearchContextPack | OpportunityContextPack | OutreachContextPack | GmailContextPack
)
