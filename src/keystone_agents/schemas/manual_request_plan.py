"""Structured planning schema for manual Keystone agent requests."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints

ManualTargetAgent = Literal[
    "orchestrator",
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "chief_of_staff",
    "airtable_context_agent",
    "google_workspace_context_agent",
    "zotero_context_agent",
    "rss_context_agent",
    "preprints_context_agent",
    "clarification",
]

ManualRequestIntent = Literal[
    "route_request",
    "company_research",
    "research_brief",
    "opportunity_search",
    "opportunity_to_outreach_loop",
    "gmail_triage",
    "outreach_draft",
    "slack_operations",
    "browser_diagnostics",
    "reference_capture",
    "business_system_write",
    "context_lookup",
    "continue_work_item",
    "blocked_send",
    "clarification",
]

ManualTaskObjective = Literal[
    "route_or_continue",
    "entity_research",
    "source_research",
    "opportunity_discovery",
    "contact_discovery",
    "outreach_draft",
    "gmail_triage",
    "slack_operations",
    "browser_diagnostics",
    "reference_capture",
    "business_system_write",
    "context_lookup",
    "blocked_side_effect",
    "clarification",
]

ManualExpectedArtifactType = Literal[
    "none",
    "research_brief",
    "source_summary",
    "opportunity_record",
    "contact_candidates",
    "outreach_draft",
    "gmail_triage_report",
    "slack_ops_summary",
    "browser_diagnostics_report",
    "reference_note",
    "business_system_write_plan",
    "context_summary",
]

ManualTargetType = Literal[
    "company",
    "person",
    "institute",
    "conference",
    "zotero_collection",
    "zotero_article",
    "article_collection",
    "gmail_thread",
    "topic",
    "opportunity",
    "operator_reference",
    "business_system_context",
    "local_document_collection",
    "slack_channel",
    "url",
    "unknown",
]

ManualProviderSystem = Literal[
    "unspecified",
    "google_calendar",
    "gmail",
    "airtable",
    "google_workspace",
    "zotero",
    "slack",
]

ManualProviderOperation = Literal[
    "read",
    "search",
    "create",
    "update",
    "delete",
    "attach",
    "verify",
]

AskBreadth = Literal["unspecified", "narrow", "bounded", "broad"]
EvidenceDepth = Literal["unspecified", "quick", "standard", "deep"]
StrictFilterMode = Literal["unspecified", "exact", "strict", "flexible"]
OutputForm = Literal["unspecified", "brief", "bullets", "table", "plan", "draft"]
PriorContextDependency = Literal["unspecified", "none", "selected_context", "required"]
PermissionState = Literal["unspecified", "read_only", "draft_only", "approval_required"]
CostMode = Literal["unspecified", "minimize", "balanced", "quality"]


class AskShapePolicy(BaseModel):
    """Orthogonal operator constraints that must survive routing and handoffs."""

    ask_breadth: AskBreadth = "unspecified"
    evidence_depth: EvidenceDepth = "unspecified"
    source_type_preference: list[str] = Field(default_factory=list)
    strict_filter_mode: StrictFilterMode = "unspecified"
    output_form: OutputForm = "unspecified"
    prior_context_dependency: PriorContextDependency = "unspecified"
    permission_state: PermissionState = "unspecified"
    cost_mode: CostMode = "unspecified"
    stop_condition: str = ""
    output_constraints: InterpretedOutputConstraints = Field(
        default_factory=InterpretedOutputConstraints
    )

    @field_validator("source_type_preference", mode="before")
    @classmethod
    def _clean_source_types(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                str(item).replace("\u2014", "-").strip().lower()
                for item in values
                if str(item or "").strip()
            )
        )

    @field_validator("stop_condition", mode="before")
    @classmethod
    def _clean_stop_condition(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class ManualRequestPlan(BaseModel):
    """Pre-execution semantic plan for manual CLI and Slack agent calls."""

    source: str = "heuristic"
    requested_agent: ManualTargetAgent | None = None
    target_agent: ManualTargetAgent = "clarification"
    workflow: list[ManualTargetAgent] = Field(default_factory=list)
    intent: ManualRequestIntent = "clarification"
    primary_target: str = ""
    target_type: ManualTargetType = "unknown"
    provider_system: ManualProviderSystem = "unspecified"
    provider_operations: list[ManualProviderOperation] = Field(default_factory=list)
    objective: str = ""
    task_objective: ManualTaskObjective = "clarification"
    expected_artifact_type: ManualExpectedArtifactType = "none"
    desired_count: int = Field(default=1, ge=1, le=10)
    constraints: list[str] = Field(default_factory=list)
    ask_shape: AskShapePolicy = Field(default_factory=AskShapePolicy)
    required_entities: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    gmail_query: str = ""
    lookback_days: int | None = None
    draft_policy: str = ""
    recipient: str = ""
    outreach_channel: str = ""
    tone: str = ""
    requires_live_search: bool = False
    requires_approved_context: bool = False
    requires_durable_state: bool = False
    missing_required_information: list[str] = Field(default_factory=list)
    side_effect_policy: str = "draft_or_read_only"
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "workflow",
        mode="before",
    )
    @classmethod
    def _clean_workflow(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        allowed = set(get_args(ManualTargetAgent))
        normalized = [
            str(item or "").strip().lower().replace(" ", "_")
            for item in values
        ]
        return list(
            dict.fromkeys(
                item
                for item in normalized
                if item in allowed
                and item not in {"orchestrator", "chief_of_staff", "clarification"}
            )
        )

    @field_validator("provider_operations", mode="before")
    @classmethod
    def _clean_provider_operations(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        allowed = set(get_args(ManualProviderOperation))
        normalized = [
            str(item or "").strip().lower().replace(" ", "_")
            for item in values
        ]
        return list(dict.fromkeys(item for item in normalized if item in allowed))

    @field_validator("missing_required_information", mode="before")
    @classmethod
    def _clean_missing_required_information(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                str(item or "").replace("\u2014", "-").strip()
                for item in values
                if str(item or "").strip()
            )
        )[:8]

    @field_validator(
        "source",
        "primary_target",
        "objective",
        "gmail_query",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
        "side_effect_policy",
        "rationale",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("lookback_days", mode="before")
    @classmethod
    def _clean_lookback_days(cls, value: object) -> int | None:
        if value in (None, ""):
            return None
        try:
            return max(1, min(365, int(value)))
        except (TypeError, ValueError):
            return None

    @field_validator(
        "constraints",
        "required_entities",
        "required_terms",
        "planner_warnings",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("desired_count", mode="before")
    @classmethod
    def _clean_desired_count(cls, value: object) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 1
