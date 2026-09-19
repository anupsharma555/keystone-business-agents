"""Structured output schema for the KNI Chief of Staff agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationInventoryReport,
    ChiefOfStaffWriteRequest,
)
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.handoff_types import HandoffTypeContract
from keystone_agents.schemas.memory import ChiefOfStaffMemoryContext

ChiefOfStaffMode = Literal["deterministic", "llm", "llm_unavailable"]
ChiefOfStaffSlackPostPolicy = Literal[
    "not_allowed",
    "draft_only",
    "channel_policy_allowed",
    "requires_human_review",
]
ChiefOfStaffWorkflowType = Literal[
    "calendar-read",
    "gmail-summary",
    "gmail-triage",
    "business-agents-route",
    "slack-runtime-review",
    "slack-cross-channel-review",
    "slack-article-review",
    "slack-follow-up-review",
    "slack-docs-review",
    "slack-command",
    "project-context-review",
    "research-direction-review",
    "budget-resource-review",
    "meeting-prep",
    "portfolio-review",
    "artifact-write-plan",
    "google-drive-management",
    "reference-capture",
    "memory-review",
    "clarification",
]
ChiefDurableHandoffAgent = Literal[
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
]
ChiefContextHandoffAgent = Literal[
    "rss_context_agent",
    "preprints_context_agent",
    "zotero_context_agent",
    "airtable_context_agent",
    "google_workspace_context_agent",
]
ChiefContextHandoffStage = Literal[
    "before_durable_handoff",
    "after_durable_handoff",
]


class ChiefSlackCommandResolution(BaseModel):
    """Compact Chief decision for routing one natural-language ask to KS."""

    status: Literal["matched", "no_match", "clarification"] = "no_match"
    command_text: str = ""
    rationale: str = ""
    confidence: Literal["high", "medium", "low"] = "low"

    @field_validator("command_text", "rationale", mode="before")
    @classmethod
    def _clean_resolution_fields(cls, value: object) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _validate_matched_command(self) -> ChiefSlackCommandResolution:
        if self.status == "matched" and not self.command_text.startswith("/kni"):
            raise ValueError("Matched Chief Slack commands must start with /kni.")
        if self.status != "matched" and self.command_text:
            raise ValueError("Only matched resolutions may include command_text.")
        return self


def _clean_text(value: object) -> str:
    """Return prompt-safe single-value text."""

    return str(value or "").replace("\u2014", "-").strip()


def _clean_list(value: object) -> list[str]:
    """Normalize a loose model list field into stripped strings."""

    if value is None:
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    return [_clean_text(item) for item in values if _clean_text(item)]


class ChiefOfStaffSourceRef(BaseModel):
    """One source used for an operational recommendation."""

    title: str = ""
    url: str = ""
    source_type: str = "local"
    note: str = ""

    @field_validator("title", "url", "source_type", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


class ChiefSpecialistContextEntry(BaseModel):
    """One compact context value passed from Chief to a specialist tool."""

    model_config = ConfigDict(extra="forbid")

    key: str = ""
    value: str = ""
    note: str = ""

    @field_validator("key", "value", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


def _context_entries(value: object) -> list[ChiefSpecialistContextEntry]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [
            ChiefSpecialistContextEntry(key=str(key), value=str(item))
            for key, item in value.items()
            if _clean_text(key) or _clean_text(item)
        ]
    values = value if isinstance(value, list | tuple | set) else [value]
    entries: list[ChiefSpecialistContextEntry] = []
    for item in values:
        if isinstance(item, ChiefSpecialistContextEntry):
            entries.append(item)
        elif isinstance(item, Mapping):
            entries.append(ChiefSpecialistContextEntry.model_validate(item))
        elif _clean_text(item):
            entries.append(ChiefSpecialistContextEntry(value=_clean_text(item)))
    return entries


class ChiefSpecialistToolInput(BaseModel):
    """Structured Chief-to-specialist input for agents-as-tools calls."""

    model_config = ConfigDict(extra="forbid")

    raw_operator_request: str = Field(
        default="",
        description="Original operator request before Chief of Staff summarization.",
    )
    specialist_task: str = Field(
        default="",
        description="Bounded question or task the specialist should answer.",
    )
    manager_context: list[ChiefSpecialistContextEntry] = Field(
        default_factory=list,
        description="Compact Chief of Staff context, such as Slack channel, thread, or run state.",
    )
    decision_context: list[ChiefSpecialistContextEntry] = Field(
        default_factory=list,
        description=(
            "Decision-useful Chief context such as intent family, desired deliverable, "
            "audience, time window, urgency, success criteria, and open questions."
        ),
    )
    target_context: list[ChiefSpecialistContextEntry] = Field(
        default_factory=list,
        description=(
            "Known target entities and system objects, such as companies, contacts, "
            "Gmail threads, Airtable bases/tables/records, Drive folders/files, "
            "Sheets tabs, Zotero collections/items, or artifact names."
        ),
    )
    coordination_context: list[ChiefSpecialistContextEntry] = Field(
        default_factory=list,
        description=(
            "How this specialist call fits with sibling specialists, including "
            "planned specialists, prior nested outputs, dependencies, and merge needs."
        ),
    )
    provider_call_context: list[ChiefSpecialistContextEntry] = Field(
        default_factory=list,
        description=(
            "Provider/tool-call hints derived from the user query, such as intended "
            "Google, Gmail, Airtable, Slack, or Zotero operation, query/filter, object "
            "IDs, date window, folder path, document/sheet/tab names, fields, label "
            "actions, row keys, source basis, and approval reference/status."
        ),
    )
    slack_context: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    gmail_context: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    work_item_context: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    source_layer_manifest: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    source_refs: list[ChiefOfStaffSourceRef] = Field(default_factory=list)
    approval_context: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    side_effect_boundaries: list[str] = Field(
        default_factory=lambda: [
            "nested_specialist_advisory_only",
            "no_send",
            "no_publish",
            "no_nested_live_write",
        ]
    )
    expected_output: str = Field(
        default=(
            "Return structured specialist context, sources, blockers, approval needs, "
            "human work context, and recommendations for Chief of Staff synthesis."
        )
    )

    @field_validator("raw_operator_request", "specialist_task", "expected_output", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("side_effect_boundaries", mode="before")
    @classmethod
    def _clean_list_fields(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator(
        "manager_context",
        "decision_context",
        "target_context",
        "coordination_context",
        "provider_call_context",
        "slack_context",
        "gmail_context",
        "work_item_context",
        "source_layer_manifest",
        "approval_context",
        mode="before",
    )
    @classmethod
    def _clean_context_entries(cls, value: object) -> list[ChiefSpecialistContextEntry]:
        return _context_entries(value)

    @model_validator(mode="after")
    def _enforce_nested_boundaries(self) -> ChiefSpecialistToolInput:
        required = {
            "nested_specialist_advisory_only",
            "no_send",
            "no_publish",
            "no_nested_live_write",
        }
        existing = set(self.side_effect_boundaries)
        self.side_effect_boundaries = [
            *self.side_effect_boundaries,
            *(item for item in sorted(required - existing)),
        ]
        return self


class ChiefNestedSpecialistSourceRef(BaseModel):
    """One source reference surfaced by a nested specialist tool."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = ""
    title: str = ""
    url: str = ""
    source_type: str = ""
    note: str = ""

    @field_validator("source_id", "title", "url", "source_type", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


class ChiefNestedSpecialistResult(BaseModel):
    """Reviewable envelope for one nested specialist-as-tool result."""

    model_config = ConfigDict(extra="forbid")

    route_name: str = ""
    tool_name: str = ""
    parsed_output_status: Literal["parsed", "text", "missing", "malformed"] = "missing"
    output_type: str = ""
    type_contract: HandoffTypeContract = Field(default_factory=HandoffTypeContract)
    source_output_type: str = ""
    target_input_type: str = ""
    payload_mode: str = "adapted"
    type_compatibility_status: str = "unknown"
    summary: str = ""
    source_ids: list[str] = Field(default_factory=list)
    source_refs: list[ChiefNestedSpecialistSourceRef] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    approval_needs: list[str] = Field(default_factory=list)
    human_work_context: list[ChiefSpecialistContextEntry] = Field(default_factory=list)
    validation_status: Literal["ok", "needs_review", "blocked"] = "needs_review"
    diagnostics: list[ChiefSpecialistContextEntry] = Field(default_factory=list)

    @field_validator(
        "route_name",
        "tool_name",
        "output_type",
        "source_output_type",
        "target_input_type",
        "payload_mode",
        "type_compatibility_status",
        "summary",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("source_ids", "blockers", "approval_needs", mode="before")
    @classmethod
    def _clean_list_fields(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("human_work_context", mode="before")
    @classmethod
    def _clean_human_work_context(
        cls,
        value: object,
    ) -> list[ChiefSpecialistContextEntry]:
        return _context_entries(value)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _clean_diagnostics(
        cls,
        value: object,
    ) -> list[ChiefSpecialistContextEntry]:
        return _context_entries(value)


class ChiefOfStaffRouteRecommendation(BaseModel):
    """A proposed Slack workflow route that still needs human approval."""

    workflow_type: ChiefOfStaffWorkflowType = "clarification"
    command_text: str = ""
    target_channel: str = ""
    rationale: str = ""
    requires_live_connector: bool = False
    requires_human_approval_before_post: bool = True

    @field_validator("command_text", "target_channel", "rationale", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


class ChiefDurableHandoff(BaseModel):
    """A durable WorkItem handoff selected by Chief of Staff."""

    agent: ChiefDurableHandoffAgent
    rationale: str = ""
    requires_approval: bool = False

    @field_validator("rationale", mode="before")
    @classmethod
    def _clean_rationale(cls, value: object) -> str:
        return _clean_text(value)


class ChiefContextHandoff(BaseModel):
    """A read-only context-agent handoff selected by Chief of Staff."""

    agent: ChiefContextHandoffAgent
    stage: ChiefContextHandoffStage = "before_durable_handoff"
    before_agent: ChiefDurableHandoffAgent | None = None
    rationale: str = ""
    requires_approval: bool = False

    @field_validator("rationale", mode="before")
    @classmethod
    def _clean_rationale(cls, value: object) -> str:
        return _clean_text(value)


class ChiefOfStaffResult(BaseModel):
    """Structured Chief of Staff recommendation with gated operating writes."""

    agent_name: str = "chief_of_staff"
    mode: ChiefOfStaffMode = "deterministic"
    intent: str = ""
    summary: str = ""
    synthesis: str = ""
    time_window: str = ""
    target_channels: list[str] = Field(default_factory=list)
    operating_capabilities: list[str] = Field(default_factory=list)
    recommended_route: ChiefOfStaffRouteRecommendation = Field(
        default_factory=ChiefOfStaffRouteRecommendation
    )
    durable_handoff: ChiefDurableHandoff | None = None
    context_handoffs: list[ChiefContextHandoff] = Field(default_factory=list)
    provider_context_decisions: list[AgentDecisionRecord] = Field(default_factory=list)
    decision: AgentDecisionRecord = Field(
        default_factory=lambda: AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="chief_delegation_selection",
            needs_more_context=True,
        )
    )
    recommended_actions: list[str] = Field(default_factory=list)
    blocked_side_effects: list[str] = Field(
        default_factory=lambda: [
            "unscoped_slack_post",
            "gmail_send",
            "calendar_create_or_update",
            "repo_write",
            "linkedin_publish",
            "crm_write",
        ]
    )
    approval_required: bool = True
    human_review_required: bool = True
    send_enabled: bool = False
    slack_post_allowed: bool = False
    slack_post_policy: ChiefOfStaffSlackPostPolicy = "not_allowed"
    slack_target_channel: str = ""
    slack_post_reason: str = ""
    sources: list[ChiefOfStaffSourceRef] = Field(default_factory=list)
    context_sources_considered: list[str] = Field(default_factory=list)
    repo_context_used: list[str] = Field(default_factory=list)
    automation_report: AutomationInventoryReport | None = None
    memory_context: ChiefOfStaffMemoryContext | None = None
    write_requests: list[ChiefOfStaffWriteRequest] = Field(default_factory=list)
    artifact_refs: list[AutomationArtifactRef] = Field(default_factory=list)
    nested_specialist_results: list[ChiefNestedSpecialistResult] = Field(default_factory=list)
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("agent_name", "intent", "summary", "synthesis", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "recommended_actions",
        "blocked_side_effects",
        "context_sources_considered",
        "repo_context_used",
        "audit_notes",
        "target_channels",
        "operating_capabilities",
        mode="before",
    )
    @classmethod
    def _clean_list_fields(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("approval_required", "human_review_required")
    @classmethod
    def _review_required(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("Chief of Staff recommendations require human review and approval.")
        return value

    @field_validator("send_enabled")
    @classmethod
    def _email_send_blocked(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("Chief of Staff cannot send email or external messages directly.")
        return value

    @field_validator(
        "slack_post_policy",
        "slack_target_channel",
        "slack_post_reason",
        mode="before",
    )
    @classmethod
    def _clean_slack_policy_fields(cls, value: object) -> str:
        return _clean_text(value)

    @model_validator(mode="after")
    def _enforce_blocked_outputs(self) -> ChiefOfStaffResult:
        route_channel = self.recommended_route.target_channel.strip()
        target_channel = self.slack_target_channel.strip() or route_channel
        if self.slack_post_allowed:
            if self.slack_post_policy != "channel_policy_allowed":
                raise ValueError(
                    "slack_post_allowed requires slack_post_policy='channel_policy_allowed'."
                )
            if not target_channel:
                raise ValueError("slack_post_allowed requires a target Slack channel.")
            if "unscoped_slack_post" in self.blocked_side_effects:
                raise ValueError(
                    "slack_post_allowed cannot leave unscoped_slack_post in blocked_side_effects."
                )
        elif self.slack_post_policy == "channel_policy_allowed":
            raise ValueError("channel_policy_allowed requires slack_post_allowed=True.")

        if (
            self.recommended_route.requires_human_approval_before_post is not True
            and not self.slack_post_allowed
        ):
            raise ValueError(
                "Slack routing can skip human approval only for channel-policy-allowed posts."
            )
        return self
