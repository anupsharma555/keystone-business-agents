"""Orchestrator routing schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.decision_trace import DecisionTrace
from keystone_agents.schemas.feedback import OperatorFeedbackRequest
from keystone_agents.schemas.handoff_types import HandoffTypeContract
from keystone_agents.schemas.retrieval import RetrievalHint

RouteName = Literal[
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
RoutingMode = Literal["deterministic", "llm", "llm_unavailable"]
OutputReviewDimension = Literal["structure", "tone", "readability", "relevance"]
OutputReviewStatus = Literal["pass", "partial", "fail"]
OutputReviewMode = Literal["deterministic", "llm", "llm_unavailable"]


class HandoffSpec(BaseModel):
    """Intended SDK handoff contract for a specialist agent."""

    contract_version: str = "keystone.handoff_spec.v1"
    route: RouteName
    agent_name: str
    builder: str
    description: str
    input_contract_type: str = ""
    input_schema: str = ""
    output_contract_type: str = ""
    output_schema: str = ""
    type_contract: HandoffTypeContract = Field(default_factory=HandoffTypeContract)


class OrchestratorArtifactField(BaseModel):
    """One draft artifact field the orchestrator can surface without writing externally."""

    name: str = ""
    value: str = ""

    @field_validator("name", "value", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class OrchestratorArtifacts(BaseModel):
    """Strict-schema artifact identifiers and draft-only metadata."""

    object_id: str = ""
    draft_id: str = ""
    opportunity_id: str = ""
    company_id: str = ""
    message_id: str = ""
    latest_artifact_id: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    state_gate: str = ""
    resume_artifact: str = ""
    resumed_from_route: str = ""
    input_type: str = ""
    crm_ready_fields: list[OrchestratorArtifactField] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        "object_id",
        "draft_id",
        "opportunity_id",
        "company_id",
        "message_id",
        "latest_artifact_id",
        "state_gate",
        "resume_artifact",
        "resumed_from_route",
        "input_type",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("risk_flags", "notes", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values: list[object] = [part for part in value.split(",")]
        else:
            values = list(value) if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("crm_ready_fields", mode="before")
    @classmethod
    def _clean_crm_ready_fields(cls, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [{"name": key, "value": item} for key, item in value.items()]
        return list(value) if isinstance(value, list | tuple) else [value]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def keys(self) -> list[str]:
        return [
            key
            for key, value in self.model_dump(mode="json").items()
            if value not in ("", [], {}, None)
        ]


class OrchestratorWorkflowStateCount(BaseModel):
    """One named workflow-state count."""

    name: str = ""
    value: int = 0

    @field_validator("name", mode="before")
    @classmethod
    def _clean_name(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class OrchestratorWorkflowStateItem(BaseModel):
    """One compact persisted workflow-state item."""

    id: str = ""
    object_type: str = ""
    object_id: str = ""
    status: str = ""
    source_agent: str = ""
    title: str = ""
    summary: str = ""
    company: str = ""
    url: str = ""
    type: str = ""
    next_step: str = ""
    route: str = ""
    target_agent: str = ""
    automation_id: str = ""
    workflow: str = ""
    schedule: str = ""
    channel: str = ""
    default_channel: str = ""
    purpose: str = ""
    created_at: str = ""
    fit_summary: str = ""
    subject: str = ""
    contact: str = ""
    approval_state: str = ""
    fit_score: float | None = None
    confidence_score: float | None = None
    priority_score: float | None = None

    @field_validator(
        "id",
        "object_type",
        "object_id",
        "status",
        "source_agent",
        "title",
        "summary",
        "company",
        "url",
        "type",
        "next_step",
        "route",
        "target_agent",
        "automation_id",
        "workflow",
        "schedule",
        "channel",
        "default_channel",
        "purpose",
        "created_at",
        "fit_summary",
        "subject",
        "contact",
        "approval_state",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class OrchestratorSlackContextMessage(BaseModel):
    """One compact Slack message reference in orchestrator-visible context."""

    ts: str = ""
    author: str = ""
    text: str = ""
    permalink: str = ""

    @field_validator("ts", "author", "text", "permalink", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()


class OrchestratorSlackContext(BaseModel):
    """Strict-schema Slack context summary for Orchestrator SDK output."""

    channel_id: str = ""
    channel_name: str = ""
    message_ts: str = ""
    thread_ts: str = ""
    selected_message_ts: str = ""
    permalink: str = ""
    latest_user_follow_up: str = ""
    thread_fetch_status: str = ""
    thread_transcript: str = ""
    read_context: str = ""
    channel_history_policy: str = ""
    context_window_days: int | None = None
    warnings: list[str] = Field(default_factory=list)
    selected_message: OrchestratorSlackContextMessage | None = None
    thread_messages: list[OrchestratorSlackContextMessage] = Field(default_factory=list)

    @field_validator(
        "channel_id",
        "channel_name",
        "message_ts",
        "thread_ts",
        "selected_message_ts",
        "permalink",
        "latest_user_follow_up",
        "thread_fetch_status",
        "thread_transcript",
        "read_context",
        "channel_history_policy",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("warnings", mode="before")
    @classmethod
    def _clean_warnings(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = list(value) if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("thread_messages", mode="before")
    @classmethod
    def _clean_thread_messages(cls, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        if isinstance(value, Mapping):
            return [value]
        return [{"text": value}]

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class OrchestratorWorkflowStateSummary(BaseModel):
    """Strict-schema summary of local workflow state available to the orchestrator."""

    counts: list[OrchestratorWorkflowStateCount] = Field(default_factory=list)
    pending_approvals: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    approved_approval_items: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    companies: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    opportunities: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    outreach_drafts: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    prior_route_decisions: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    recent_slack_thread: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    prior_agent_runs: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    channel_automations: list[OrchestratorWorkflowStateItem] = Field(default_factory=list)
    slack_context: OrchestratorSlackContext = Field(default_factory=OrchestratorSlackContext)
    approved_context_available: bool = False
    send_enabled: bool = False

    @field_validator("counts", mode="before")
    @classmethod
    def _clean_counts(cls, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            return [{"name": key, "value": item} for key, item in value.items()]
        return list(value) if isinstance(value, list | tuple) else [value]

    @field_validator(
        "pending_approvals",
        "approved_approval_items",
        "companies",
        "opportunities",
        "outreach_drafts",
        "prior_route_decisions",
        "recent_slack_thread",
        "prior_agent_runs",
        "channel_automations",
        mode="before",
    )
    @classmethod
    def _clean_items(cls, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return list(value)
        if isinstance(value, Mapping):
            return [value]
        return [{"summary": value}]

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


class OrchestratorResult(BaseModel):
    """Structured route decision with deterministic safety gates."""

    route: RouteName = "clarification"
    target_agent: str | None = None
    workflow: list[str] = Field(default_factory=list)
    routing_mode: RoutingMode = "deterministic"
    rationale: str = ""
    clarification_request: str | None = None
    stop_reason: str | None = None
    requires_human_review: bool = True
    approval_required: bool = True
    approval_state: ApprovalState = ApprovalState.PENDING
    approval_scope: ApprovalScope = ApprovalScope.DRAFTING
    approval_rationale: str = ""
    external_use_approval_required: bool = True
    approved_context_present: bool = False
    refused: bool = False
    send_enabled: bool = False
    can_send_email: bool = False
    forbidden_actions: list[str] = Field(default_factory=lambda: ["send_email"])
    intended_handoffs: list[HandoffSpec] = Field(default_factory=list)
    retrieval_hint: RetrievalHint | None = None
    retrieval_diagnostics: SkipJsonSchema[dict[str, Any]] = Field(default_factory=dict)
    artifacts: OrchestratorArtifacts = Field(default_factory=OrchestratorArtifacts)
    state_context_used: bool = False
    workflow_state_summary: OrchestratorWorkflowStateSummary = Field(
        default_factory=OrchestratorWorkflowStateSummary
    )
    operator_feedback_request: OperatorFeedbackRequest | None = None
    decision_trace: DecisionTrace | None = None
    audit_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_draft_only_crm_fields_when_requested(self) -> OrchestratorResult:
        crm_requested = any("crm" in str(item).lower() for item in self.workflow)
        crm_requested = crm_requested or any(
            "crm" in str(item).lower() for item in self.forbidden_actions
        )
        if crm_requested and not self.artifacts.crm_ready_fields:
            target = self.artifacts.company_id or ""
            self.artifacts.crm_ready_fields = [
                OrchestratorArtifactField(name="company", value=target or "needs confirmation"),
                OrchestratorArtifactField(name="contact_name", value="needs confirmation"),
                OrchestratorArtifactField(name="contact_email", value="needs confirmation"),
                OrchestratorArtifactField(
                    name="outreach_status",
                    value="draft_only_blocked_pending_approval",
                ),
                OrchestratorArtifactField(
                    name="recommended_next_step",
                    value=self.clarification_request
                    or self.stop_reason
                    or "confirm target and approvals before any CRM write",
                ),
            ]
            self.artifacts.notes = list(
                dict.fromkeys(
                    [
                        *self.artifacts.notes,
                        "CRM fields are draft-only planning data and were not written externally.",
                    ]
                )
            )
        if self.route == "business_research_analyst":
            company = self.artifacts.company_id
            for field in self.artifacts.crm_ready_fields:
                confirmed_company = (
                    field.name == "company"
                    and field.value
                    and "needs confirmation" not in field.value
                )
                if confirmed_company:
                    company = field.value
                    break
            next_step = (
                f"Next safe agent call: run Business Research Analyst for {company}."
                if company
                else (
                    "Next safe agent call: run Business Research Analyst after target confirmation."
                )
            )
            self.artifacts.notes = list(dict.fromkeys([*self.artifacts.notes, next_step]))
        return self


class OrchestratorOutputReviewScore(BaseModel):
    """One dimension of an orchestrator-owned specialist output review."""

    dimension: OutputReviewDimension
    score: int = Field(ge=0, le=100)
    status: OutputReviewStatus
    rationale: str = ""
    suggestions: list[str] = Field(default_factory=list)

    @field_validator("rationale", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("suggestions", mode="before")
    @classmethod
    def _clean_suggestions(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]


class OrchestratorReviewBaseline(BaseModel):
    """Strict-schema deterministic baseline carried alongside LLM review."""

    status: str = ""
    overall_score: int = 0
    structure: str = ""
    tone: str = ""
    readability: str = ""
    relevance: str = ""
    approval_boundary_ok: bool = True

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class OrchestratorReviewCostGuard(BaseModel):
    """Strict-schema cost and payload guard metadata for LLM review."""

    mode: str = ""
    model_call: bool = False
    full_body_fields_omitted: list[str] = Field(default_factory=list)
    max_serialized_chars: int = 0
    max_string_chars: int = 0
    max_list_items: int = 0
    max_mapping_keys: int = 0
    scope: str = ""
    deterministic_hard_gates_authoritative: bool = False
    status_policy: str = ""

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class OrchestratorReviewChecks(BaseModel):
    """Strict-schema review check statuses."""

    structure: str = ""
    tone: str = ""
    readability: str = ""
    relevance: str = ""
    preserves_no_send_behavior: str = ""
    raw_llm_review_status: str = ""
    deterministic_review_status: str = ""
    hybrid_final_status: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> OrchestratorReviewChecks:
        return cls(
            structure=str(value.get("Structure") or value.get("structure") or ""),
            tone=str(value.get("Tone") or value.get("tone") or ""),
            readability=str(value.get("Readability") or value.get("readability") or ""),
            relevance=str(value.get("Relevance") or value.get("relevance") or ""),
            preserves_no_send_behavior=str(
                value.get("Preserves no-send behavior")
                or value.get("preserves_no_send_behavior")
                or ""
            ),
            raw_llm_review_status=str(
                value.get("Raw LLM review status") or value.get("raw_llm_review_status") or ""
            ),
            deterministic_review_status=str(
                value.get("Deterministic review status")
                or value.get("deterministic_review_status")
                or ""
            ),
            hybrid_final_status=str(
                value.get("Hybrid final status") or value.get("hybrid_final_status") or ""
            ),
        )

    def __getitem__(self, key: str) -> Any:
        aliases = {
            "Structure": "structure",
            "Tone": "tone",
            "Readability": "readability",
            "Relevance": "relevance",
            "Preserves no-send behavior": "preserves_no_send_behavior",
            "Raw LLM review status": "raw_llm_review_status",
            "Deterministic review status": "deterministic_review_status",
            "Hybrid final status": "hybrid_final_status",
        }
        return getattr(self, aliases.get(key, key))

    def keys(self) -> list[str]:
        return [
            "Structure",
            "Tone",
            "Readability",
            "Relevance",
            "Preserves no-send behavior",
            "Raw LLM review status",
            "Deterministic review status",
            "Hybrid final status",
        ]

    def __iter__(self):
        return iter(self.keys())


class OrchestratorOutputReview(BaseModel):
    """Dry-run-safe quality review for a completed specialist agent output."""

    reviewed_by: str = "orchestrator"
    agent_name: str
    output_type: str = ""
    review_mode: OutputReviewMode = "deterministic"
    overall_score: int = Field(ge=0, le=100)
    status: OutputReviewStatus
    structure: OrchestratorOutputReviewScore
    tone: OrchestratorOutputReviewScore
    readability: OrchestratorOutputReviewScore
    relevance: OrchestratorOutputReviewScore
    human_readable: bool = True
    metadata_relevance_ok: bool = True
    approval_boundary_ok: bool = True
    send_enabled: bool = False
    can_send_email: bool = False
    llm_review_used: bool = False
    deterministic_baseline: OrchestratorReviewBaseline = Field(
        default_factory=OrchestratorReviewBaseline
    )
    cost_guard: OrchestratorReviewCostGuard = Field(default_factory=OrchestratorReviewCostGuard)
    observed_gaps: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    recommended_next_step: str = ""
    test_pack_checks: OrchestratorReviewChecks = Field(default_factory=OrchestratorReviewChecks)
    audit_notes: list[str] = Field(default_factory=list)

    @field_validator("agent_name", "output_type", "recommended_next_step", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("observed_gaps", "strengths", "audit_notes", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("deterministic_baseline", mode="before")
    @classmethod
    def _clean_baseline(cls, value: object) -> OrchestratorReviewBaseline | object:
        if isinstance(value, dict):
            return OrchestratorReviewBaseline(
                status=str(value.get("status") or ""),
                overall_score=int(value.get("overall_score") or 0),
                structure=str(value.get("structure") or ""),
                tone=str(value.get("tone") or ""),
                readability=str(value.get("readability") or ""),
                relevance=str(value.get("relevance") or ""),
                approval_boundary_ok=bool(value.get("approval_boundary_ok", True)),
            )
        return value

    @field_validator("cost_guard", mode="before")
    @classmethod
    def _clean_cost_guard(cls, value: object) -> OrchestratorReviewCostGuard | object:
        if isinstance(value, dict):
            return OrchestratorReviewCostGuard(
                mode=str(value.get("mode") or ""),
                model_call=bool(value.get("model_call", False)),
                full_body_fields_omitted=[
                    str(item) for item in value.get("full_body_fields_omitted") or [] if str(item)
                ],
                max_serialized_chars=int(value.get("max_serialized_chars") or 0),
                max_string_chars=int(value.get("max_string_chars") or 0),
                max_list_items=int(value.get("max_list_items") or 0),
                max_mapping_keys=int(value.get("max_mapping_keys") or 0),
                scope=str(value.get("scope") or ""),
                deterministic_hard_gates_authoritative=bool(
                    value.get("deterministic_hard_gates_authoritative", False)
                ),
                status_policy=str(value.get("status_policy") or ""),
            )
        return value

    @field_validator("test_pack_checks", mode="before")
    @classmethod
    def _clean_test_pack_checks(cls, value: object) -> OrchestratorReviewChecks | object:
        if isinstance(value, dict):
            return OrchestratorReviewChecks.from_mapping(value)
        return value

    @model_validator(mode="after")
    def _enforce_no_send(self) -> OrchestratorOutputReview:
        if self.send_enabled or self.can_send_email:
            raise ValueError("orchestrator output review cannot enable sending")
        return self


OrchestratorDecision = OrchestratorResult
