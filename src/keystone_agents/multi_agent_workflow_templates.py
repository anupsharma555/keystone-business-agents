"""Backend-selected multi-agent workflow templates.

These templates are planning contracts for WorkItem and scheduled automation
selection. They are not user-facing prompt flags and they do not grant live
write authority.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, Field


class WorkflowRunMode(StrEnum):
    """Supported operating modes for a multi-agent workflow template."""

    INDEPENDENT = "independent"
    COMBINED = "combined"
    SCHEDULED = "scheduled"
    MODIFICATION_LOOP = "modification_loop"


class WorkflowTriggerType(StrEnum):
    """Backend trigger families that may select a workflow."""

    MANUAL = "manual"
    SCHEDULE = "schedule"
    GMAIL_THREAD = "gmail_thread"
    WORKSPACE_CHANGE = "workspace_change"
    FEED_DELTA = "feed_delta"
    OPPORTUNITY_STATE_CHANGE = "opportunity_state_change"
    REVIEW_QUEUE = "review_queue"


class WorkflowToolTier(StrEnum):
    """Maximum tool tier a template may use before approval escalation."""

    FIXTURE_ONLY = "fixture_only"
    LOCAL_READ_ONLY = "local_read_only"
    LIVE_RETRIEVAL_READ_ONLY = "live_retrieval_read_only"
    LIVE_CONTEXT_READ_ONLY = "live_context_read_only"
    APPROVED_DRAFT_CREATION = "approved_draft_creation"
    APPROVED_INTERNAL_WRITE_PLAN = "approved_internal_write_plan"


class WorkflowHandoffContract(BaseModel):
    """State that must survive agent handoffs."""

    raw_request_required: bool = True
    work_item_state_required: bool = True
    source_refs_required: bool = True
    approval_state_required: bool = True
    context_packs: list[str] = Field(default_factory=list)
    passed_state: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)


class MultiAgentWorkflowTemplate(BaseModel):
    """Definition for one backend-selected Keystone business workflow."""

    id: str
    name: str
    summary: str
    run_modes: list[WorkflowRunMode]
    trigger_type: WorkflowTriggerType
    cadence: str
    backend_selection_signals: list[str] = Field(default_factory=list)
    agent_chain: list[str] = Field(default_factory=list)
    handoff_contract: WorkflowHandoffContract
    tool_tier: WorkflowToolTier
    approval_gate: str
    output_destinations: list[str] = Field(default_factory=list)
    budget_stop_condition: str
    validation_path: list[str] = Field(default_factory=list)
    source_attribution_contract: str
    side_effect_boundary: str
    modification_loop: str = ""
    implementation_status: str = "candidate"


def workflow_template_catalog() -> tuple[MultiAgentWorkflowTemplate, ...]:
    """Return the first realistic workflow templates KBA should support."""

    return _WORKFLOW_TEMPLATES


def get_workflow_template(template_id: str) -> MultiAgentWorkflowTemplate | None:
    """Return one workflow template by id."""

    normalized = str(template_id or "").strip()
    for template in _WORKFLOW_TEMPLATES:
        if template.id == normalized:
            return template
    return None


def select_workflow_templates_for_backend_context(
    *,
    trigger_type: WorkflowTriggerType | str | None = None,
    available_context: Iterable[str] = (),
    requested_capabilities: Iterable[str] = (),
) -> tuple[MultiAgentWorkflowTemplate, ...]:
    """Select templates from backend state, not from operator prompt flags."""

    trigger = WorkflowTriggerType(trigger_type) if trigger_type else None
    context = {str(item).strip().lower() for item in available_context if str(item).strip()}
    capabilities = {
        str(item).strip().lower() for item in requested_capabilities if str(item).strip()
    }
    selected: list[MultiAgentWorkflowTemplate] = []
    for template in _WORKFLOW_TEMPLATES:
        if trigger is not None and template.trigger_type != trigger:
            continue
        signals = {signal.lower() for signal in template.backend_selection_signals}
        if context and not signals.intersection(context):
            continue
        if capabilities:
            searchable = {
                template.id.lower(),
                template.name.lower(),
                template.summary.lower(),
                template.trigger_type.value.lower(),
                *[agent.lower() for agent in template.agent_chain],
                *[mode.value.lower() for mode in template.run_modes],
            }
            if not any(
                capability in item or item in capability
                for capability in capabilities
                for item in searchable
            ):
                continue
        selected.append(template)
    return tuple(selected)


_WORKFLOW_TEMPLATES: tuple[MultiAgentWorkflowTemplate, ...] = (
    MultiAgentWorkflowTemplate(
        id="gmail_thread_research_opportunity_outreach",
        name="Gmail Thread To Research, Opportunity, And Outreach Draft",
        summary=(
            "Turn a high-signal Gmail thread into a sourced company/contact packet, "
            "opportunity assessment, documentation plan, and approval-gated outreach draft."
        ),
        run_modes=[
            WorkflowRunMode.COMBINED,
            WorkflowRunMode.MODIFICATION_LOOP,
        ],
        trigger_type=WorkflowTriggerType.GMAIL_THREAD,
        cadence="event-driven or operator-selected Gmail thread",
        backend_selection_signals=[
            "gmail_thread_id",
            "gmail_message_refs",
            "recipient_or_company_hint",
            "reply_requested_without_send_authority",
        ],
        agent_chain=[
            "orchestrator",
            "gmail_triage",
            "business_research_analyst",
            "opportunity_scout",
            "google_workspace_context",
            "airtable_context",
            "outreach_composer",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=[
                "GmailContextPack",
                "ResearchContextPack",
                "OpportunityContextPack",
                "OutreachContextPack",
            ],
            passed_state=[
                "raw Gmail thread refs and compact thread facts",
                "selected source refs and approved factual claims",
                "company/contact identity and ambiguity blockers",
                "opportunity score/rationale and duplicate checks",
                "drafting approval state and external-use approval state",
            ],
            blockers=[
                "missing Gmail thread identity",
                "ambiguous recipient or company identity",
                "insufficient source-backed claims",
                "missing drafting approval",
            ],
            allowed_actions=[
                "read Gmail thread context",
                "run read-only live research when enabled",
                "prepare Workspace/Airtable write plans",
                "create outreach draft only after scoped draft approval",
            ],
        ),
        tool_tier=WorkflowToolTier.APPROVED_DRAFT_CREATION,
        approval_gate=(
            "Human approval is required before Gmail draft creation; sending is never allowed."
        ),
        output_destinations=[
            "WorkItem artifacts",
            "Slack review summary",
            "Google Workspace write plan",
            "Airtable write plan",
            "Gmail draft approval item",
        ],
        budget_stop_condition=(
            "Stop after one thread, one target company, five selected sources, one opportunity "
            "record, and one draft candidate unless the operator explicitly continues."
        ),
        validation_path=[
            "fixture Gmail thread -> WorkItem context pack",
            "source attribution assertion",
            "draft approval gate assertion",
            "no-send assertion",
        ],
        source_attribution_contract=(
            "Every external claim in the opportunity record or draft rationale must map to "
            "selected source refs visible in the review output."
        ),
        side_effect_boundary=(
            "No email send, CRM write, Airtable write, Workspace write, Slack post, or schedule "
            "action without a separate scoped approval and live integration flag."
        ),
        modification_loop=(
            "Follow-up revisions may update the WorkItem draft/artifact plan while preserving "
            "the original thread refs, source refs, and approval history."
        ),
    ),
    MultiAgentWorkflowTemplate(
        id="workspace_research_packet_refresh",
        name="Workspace Research Packet Refresh",
        summary=(
            "Refresh an internal Workspace research packet from approved docs, selected web "
            "sources, and prior WorkItem facts without writing until review."
        ),
        run_modes=[WorkflowRunMode.COMBINED, WorkflowRunMode.SCHEDULED],
        trigger_type=WorkflowTriggerType.WORKSPACE_CHANGE,
        cadence="operator-selected document or scheduled monthly review",
        backend_selection_signals=[
            "google_doc_id",
            "drive_folder_id",
            "workspace_artifact_ref",
            "research_packet_stale",
        ],
        agent_chain=[
            "orchestrator",
            "google_workspace_context",
            "business_research_analyst",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=["ResearchContextPack"],
            passed_state=[
                "Workspace artifact refs and current summary",
                "approved facts and superseded-fact notes",
                "selected source refs",
                "write-plan approval state",
            ],
            blockers=[
                "missing document or folder identity",
                "unclear source basis",
                "write requested without approval",
            ],
            allowed_actions=[
                "read bounded Workspace context",
                "run source-backed research",
                "prepare a Google Doc update plan",
            ],
        ),
        tool_tier=WorkflowToolTier.APPROVED_INTERNAL_WRITE_PLAN,
        approval_gate="Human approval review is required before any Google Workspace write.",
        output_destinations=[
            "WorkItem artifacts",
            "Slack review summary",
            "Google Doc update plan",
        ],
        budget_stop_condition=(
            "Stop after one target document/folder, ten source refs, and one update plan."
        ),
        validation_path=[
            "fixture Workspace context read",
            "stale fact replacement assertion",
            "Workspace write-plan only assertion",
        ],
        source_attribution_contract=(
            "Changed factual claims must cite either the existing Workspace artifact ref or a "
            "new selected external source ref."
        ),
        side_effect_boundary=(
            "No Google Doc, Sheet, Drive, Airtable, Slack, or email write from the template run."
        ),
    ),
    MultiAgentWorkflowTemplate(
        id="rss_zotero_preprint_research_digest",
        name="RSS, Zotero, And Preprint Research Digest",
        summary=(
            "Combine announcement/RSS deltas, Zotero library context, and preprint evidence "
            "into a short source-backed research digest."
        ),
        run_modes=[WorkflowRunMode.SCHEDULED, WorkflowRunMode.COMBINED],
        trigger_type=WorkflowTriggerType.FEED_DELTA,
        cadence="weekly, with optional urgent event-driven run for high-signal feed items",
        backend_selection_signals=[
            "rss_feed_delta",
            "announcement_items",
            "zotero_collection_context",
            "preprint_context_available",
        ],
        agent_chain=[
            "orchestrator",
            "rss_context_agent",
            "zotero_context_agent",
            "preprints_context_agent",
            "business_research_analyst",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=["ResearchContextPack"],
            passed_state=[
                "feed item refs and publication metadata",
                "Zotero collection refs",
                "preprint caveats and source maturity",
                "selected source refs and synthesis notes",
            ],
            blockers=[
                "no usable feed delta",
                "missing Zotero collection context",
                "preprint evidence too preliminary for external claim",
            ],
            allowed_actions=[
                "read local RSS/announcement context",
                "read Zotero context",
                "read preprint context",
                "run read-only source synthesis",
            ],
        ),
        tool_tier=WorkflowToolTier.LIVE_RETRIEVAL_READ_ONLY,
        approval_gate="Digest posting requires Slack review/post approval; no Zotero writes.",
        output_destinations=[
            "Slack review summary",
            "WorkItem artifacts",
            "local digest artifact",
        ],
        budget_stop_condition=(
            "Stop after five selected items, ten source refs, and one digest unless "
            "manually continued."
        ),
        validation_path=[
            "announcement fixture synthesis",
            "context-agent source ref assertion",
            "no Zotero write assertion",
            "preprint caveat assertion",
        ],
        source_attribution_contract=(
            "Each digest item must keep feed/Zotero/preprint source refs visible and "
            "distinguish peer-reviewed, preprint, and announcement-only evidence."
        ),
        side_effect_boundary=(
            "No Slack post, Zotero import/update, Workspace write, Airtable write, or "
            "outbound draft."
        ),
    ),
    MultiAgentWorkflowTemplate(
        id="weekly_opportunity_to_outreach_review",
        name="Weekly Opportunity To Outreach Review",
        summary=(
            "Run a weekly opportunity scan, enrich the top candidates, prepare contact paths, "
            "and stop at a human outreach review checkpoint."
        ),
        run_modes=[WorkflowRunMode.SCHEDULED, WorkflowRunMode.COMBINED],
        trigger_type=WorkflowTriggerType.SCHEDULE,
        cadence="weekly",
        backend_selection_signals=[
            "weekly_opportunity_schedule",
            "opportunity_watch_topics",
            "prior_opportunity_state",
        ],
        agent_chain=[
            "orchestrator",
            "opportunity_scout",
            "business_research_analyst",
            "outreach_composer",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=["OpportunityContextPack", "ResearchContextPack", "OutreachContextPack"],
            passed_state=[
                "opportunity records and scores",
                "source refs and selected source excerpts",
                "contact path confidence and missing info",
                "drafting approval state",
                "external-use approval state",
            ],
            blockers=[
                "weak or stale opportunity sources",
                "duplicate opportunity already tracked",
                "missing contact path",
                "drafting not approved",
            ],
            allowed_actions=[
                "run live search when enabled",
                "score and dedupe opportunities",
                "prepare draft candidates after approval",
                "queue human review",
            ],
        ),
        tool_tier=WorkflowToolTier.APPROVED_DRAFT_CREATION,
        approval_gate=(
            "Separate approval is required before drafting; external sending remains unavailable."
        ),
        output_destinations=[
            "WorkItem artifacts",
            "Slack approval queue",
            "local opportunity state",
            "Gmail draft approval item",
        ],
        budget_stop_condition=(
            "Stop after five opportunities, one contact path per opportunity, and draft candidates "
            "only for approved records."
        ),
        validation_path=[
            "weekly opportunity workflow fixture",
            "approval checkpoint assertion",
            "source link visibility assertion",
            "no-send assertion",
        ],
        source_attribution_contract=(
            "Each opportunity and contact path must retain source refs through review "
            "and draft rationale."
        ),
        side_effect_boundary=(
            "No Gmail send, LinkedIn send, CRM write, Airtable write, or Slack post "
            "beyond review output."
        ),
        implementation_status="partially_supported",
    ),
    MultiAgentWorkflowTemplate(
        id="key_email_response_queue",
        name="Key Email Response Queue",
        summary=(
            "Review important unread or labeled email, classify response needs, gather missing "
            "research, and queue draft-only response plans."
        ),
        run_modes=[WorkflowRunMode.SCHEDULED, WorkflowRunMode.COMBINED],
        trigger_type=WorkflowTriggerType.SCHEDULE,
        cadence="daily weekday review",
        backend_selection_signals=[
            "gmail_query",
            "unread_key_email_count",
            "response_queue_schedule",
        ],
        agent_chain=[
            "orchestrator",
            "gmail_triage",
            "business_research_analyst",
            "outreach_composer",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=["GmailContextPack", "ResearchContextPack", "OutreachContextPack"],
            passed_state=[
                "Gmail message refs and labels",
                "triage priority and response blockers",
                "source-backed facts for reply context",
                "drafting approval state",
            ],
            blockers=[
                "missing Gmail query scope",
                "ambiguous reply recipient",
                "unsupported factual claim",
                "missing draft approval",
            ],
            allowed_actions=[
                "read scoped Gmail metadata/content",
                "run supporting research",
                "prepare draft-only response text after approval",
            ],
        ),
        tool_tier=WorkflowToolTier.APPROVED_DRAFT_CREATION,
        approval_gate=(
            "Human approval is required before Gmail draft creation; send is not implemented."
        ),
        output_destinations=[
            "Slack review summary",
            "WorkItem artifacts",
            "Gmail draft approval item",
        ],
        budget_stop_condition=(
            "Stop after ten scoped messages, three research-backed reply plans, and one "
            "draft per approved thread."
        ),
        validation_path=[
            "Gmail triage fixture",
            "thread readiness gate assertion",
            "draft-only assertion",
            "no-send assertion",
        ],
        source_attribution_contract=(
            "Reply plans must distinguish email-thread facts from external source-backed facts."
        ),
        side_effect_boundary=(
            "No Gmail send, label mutation, archive, Slack post, CRM write, or external scheduling."
        ),
    ),
    MultiAgentWorkflowTemplate(
        id="contact_opportunity_documentation_refresh",
        name="Contact And Opportunity Documentation Refresh",
        summary=(
            "Refresh local opportunity/contact documentation from source-backed research, "
            "Workspace context, and Airtable context without mutating business systems."
        ),
        run_modes=[WorkflowRunMode.SCHEDULED, WorkflowRunMode.MODIFICATION_LOOP],
        trigger_type=WorkflowTriggerType.OPPORTUNITY_STATE_CHANGE,
        cadence="weekly or after approved opportunity-state changes",
        backend_selection_signals=[
            "opportunity_record_changed",
            "contact_context_stale",
            "airtable_context_available",
            "workspace_artifact_ref",
        ],
        agent_chain=[
            "orchestrator",
            "opportunity_scout",
            "business_research_analyst",
            "airtable_context",
            "google_workspace_context",
            "chief_of_staff",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=["OpportunityContextPack", "ResearchContextPack"],
            passed_state=[
                "existing opportunity/contact refs",
                "dedupe and identity-resolution notes",
                "approved facts and stale facts",
                "Airtable/Workspace write-plan approval state",
            ],
            blockers=[
                "ambiguous record identity",
                "conflicting source facts",
                "write requested without approval",
            ],
            allowed_actions=[
                "read local opportunity state",
                "read Airtable context when enabled",
                "read Workspace context when enabled",
                "prepare update plans",
            ],
        ),
        tool_tier=WorkflowToolTier.APPROVED_INTERNAL_WRITE_PLAN,
        approval_gate="Human approval is required before Airtable or Workspace updates.",
        output_destinations=[
            "WorkItem artifacts",
            "Airtable update plan",
            "Google Workspace update plan",
            "Slack review summary",
        ],
        budget_stop_condition=(
            "Stop after ten records, twenty source refs, and one consolidated update plan."
        ),
        validation_path=[
            "fixture opportunity state refresh",
            "record identity blocker assertion",
            "write-plan only assertion",
            "source attribution assertion",
        ],
        source_attribution_contract=(
            "Each changed field proposal must cite the source ref or existing record "
            "field that supports it."
        ),
        side_effect_boundary=(
            "No Airtable, Workspace, CRM, Slack, Gmail, or calendar writes from the template run."
        ),
    ),
    MultiAgentWorkflowTemplate(
        id="scheduled_review_retry_loop",
        name="Scheduled Review And Retry Loop",
        summary=(
            "Review open WorkItems, failed automation runs, stale approvals, and blocked drafts; "
            "then recommend the next safe retry or clarification action."
        ),
        run_modes=[WorkflowRunMode.SCHEDULED, WorkflowRunMode.MODIFICATION_LOOP],
        trigger_type=WorkflowTriggerType.REVIEW_QUEUE,
        cadence="daily lightweight review and weekly deeper review",
        backend_selection_signals=[
            "open_work_items",
            "failed_automation_runs",
            "stale_approval_items",
            "blocked_draft_items",
        ],
        agent_chain=[
            "orchestrator",
            "chief_of_staff",
            "business_research_analyst",
            "opportunity_scout",
            "gmail_triage",
            "outreach_composer",
        ],
        handoff_contract=WorkflowHandoffContract(
            context_packs=[
                "ResearchContextPack",
                "OpportunityContextPack",
                "GmailContextPack",
                "OutreachContextPack",
            ],
            passed_state=[
                "WorkItem state and timeline events",
                "approval queue state",
                "artifact refs",
                "retry diagnostics and prior blockers",
                "allowed next actions",
            ],
            blockers=[
                "missing WorkItem id",
                "approval scope unclear",
                "retry would repeat a failed live side effect",
            ],
            allowed_actions=[
                "read WorkItem and automation inventory",
                "summarize blockers",
                "prepare safe retry plans",
                "request clarification",
            ],
        ),
        tool_tier=WorkflowToolTier.LOCAL_READ_ONLY,
        approval_gate=(
            "Retry plans may be proposed automatically; any live side effect needs approval."
        ),
        output_destinations=[
            "Slack review summary",
            "WorkItem timeline note",
            "automation run note",
        ],
        budget_stop_condition=(
            "Stop after twenty queue items or the first five blocker classes needing human input."
        ),
        validation_path=[
            "automation inventory fixture",
            "stale approval assertion",
            "safe retry action assertion",
            "no side-effect assertion",
        ],
        source_attribution_contract=(
            "Review notes must reference the source WorkItem, automation run, artifact, or "
            "approval item that supports each recommendation."
        ),
        side_effect_boundary=(
            "No retry may execute live email, Slack, Airtable, Workspace, CRM, calendar, or "
            "publication actions without a new scoped approval."
        ),
        implementation_status="candidate",
    ),
)


__all__ = [
    "MultiAgentWorkflowTemplate",
    "WorkflowHandoffContract",
    "WorkflowRunMode",
    "WorkflowToolTier",
    "WorkflowTriggerType",
    "get_workflow_template",
    "select_workflow_templates_for_backend_context",
    "workflow_template_catalog",
]
