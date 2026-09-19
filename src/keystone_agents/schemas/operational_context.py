"""Structured outputs for internal operational context specialist agents."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.decision_ownership import AgentDecisionRecord


def _clean_text(value: object, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _clean_list(value: object, *, max_items: int = 20) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list | tuple | set) else [value]
    cleaned = [_clean_text(item) for item in items]
    return [item for item in cleaned if item][:max_items]


class OperationalContextEntry(BaseModel):
    """One strict-schema-safe key/value context entry."""

    model_config = ConfigDict(extra="forbid")

    key: str = ""
    value: str = ""
    note: str = ""

    @field_validator("key", "value", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


def _clean_entries(value: object, *, max_items: int = 30) -> list[OperationalContextEntry]:
    if value is None:
        return []
    if isinstance(value, dict):
        entries = [
            OperationalContextEntry(key=str(key), value=str(item))
            for key, item in value.items()
            if _clean_text(key) or _clean_text(item)
        ]
        return entries[:max_items]
    values = value if isinstance(value, list | tuple | set) else [value]
    entries: list[OperationalContextEntry] = []
    for item in values:
        if isinstance(item, OperationalContextEntry):
            entries.append(item)
        elif isinstance(item, dict):
            entries.append(OperationalContextEntry.model_validate(item))
        elif _clean_text(item):
            entries.append(OperationalContextEntry(value=_clean_text(item)))
    return entries[:max_items]


class OperationalContextSource(BaseModel):
    """One internal context source considered by a context specialist."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = ""
    title: str = ""
    source_type: str = "internal"
    location: str = ""
    note: str = ""

    @field_validator("source_id", "title", "source_type", "location", "note", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)


class HumanWorkContext(BaseModel):
    """Human work and integration context needed by Chief of Staff."""

    model_config = ConfigDict(extra="forbid")

    work_functions: list[str] = Field(default_factory=list)
    human_owner_hint: str = ""
    decision_needed: str = ""
    handoff_ready_context: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
    integration_surfaces: list[str] = Field(default_factory=list)
    follow_up_actions: list[str] = Field(default_factory=list)

    @field_validator("human_owner_hint", "decision_needed", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "work_functions",
        "handoff_ready_context",
        "missing_context",
        "integration_surfaces",
        "follow_up_actions",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)


class OperationalWritePlan(BaseModel):
    """A proposed internal write for review, not an executed nested write."""

    model_config = ConfigDict(extra="forbid")

    target_system: Literal["airtable", "google_workspace"] = "airtable"
    operation: str = "none"
    target: str = ""
    scope: str = ""
    field_mapping: list[OperationalContextEntry] = Field(default_factory=list)
    approval_required: bool = True
    approval_reference_needed: bool = True
    live_write_allowed_for_specialist: bool = False
    rationale: str = ""

    @field_validator("operation", "target", "scope", "rationale", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("field_mapping", mode="before")
    @classmethod
    def _clean_field_mapping(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @model_validator(mode="after")
    def _force_nested_write_boundary(self) -> OperationalWritePlan:
        self.live_write_allowed_for_specialist = False
        self.approval_required = True
        self.approval_reference_needed = True
        return self


class AirtableContextResult(BaseModel):
    """Airtable context, guarded direct-write results, and Chief handoff plans."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str = "airtable_context_agent"
    mode: Literal["llm", "deterministic", "llm_unavailable"] = "llm"
    summary: str = ""
    base_alias: str = ""
    base_id: str = ""
    relevant_tables: list[str] = Field(default_factory=list)
    relevant_fields: list[str] = Field(default_factory=list)
    candidate_record_ids: list[str] = Field(default_factory=list)
    record_summaries: list[OperationalContextEntry] = Field(default_factory=list)
    recommended_record_identity: str = ""
    recommended_actions: list[str] = Field(default_factory=list)
    direct_write_supported: bool = True
    executed_write_results: list[OperationalContextEntry] = Field(default_factory=list)
    write_plan: OperationalWritePlan = Field(
        default_factory=lambda: OperationalWritePlan(target_system="airtable")
    )
    blockers: list[str] = Field(default_factory=list)
    approval_needs: list[str] = Field(default_factory=list)
    human_work_context: HumanWorkContext = Field(default_factory=HumanWorkContext)
    sources: list[OperationalContextSource] = Field(default_factory=list)
    diagnostics: list[OperationalContextEntry] = Field(default_factory=list)
    decision: AgentDecisionRecord = Field(
        default_factory=lambda: AgentDecisionRecord(
            decision_stage="airtable_record_selection",
            needs_more_context=True,
        )
    )

    @field_validator(
        "agent_name",
        "mode",
        "summary",
        "base_alias",
        "base_id",
        "recommended_record_identity",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "relevant_tables",
        "relevant_fields",
        "candidate_record_ids",
        "recommended_actions",
        "blockers",
        "approval_needs",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("record_summaries", mode="before")
    @classmethod
    def _clean_record_summaries(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value, max_items=10)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _clean_diagnostics(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @field_validator("executed_write_results", mode="before")
    @classmethod
    def _clean_executed_write_results(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @model_validator(mode="after")
    def _force_agent_name_and_boundary(self) -> AirtableContextResult:
        self.agent_name = "airtable_context_agent"
        self.write_plan.target_system = "airtable"
        self.write_plan.live_write_allowed_for_specialist = False
        return self


class GoogleWorkspaceContextResult(BaseModel):
    """Google Workspace context, guarded direct-write results, and handoff plans."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str = "google_workspace_context_agent"
    mode: Literal["llm", "deterministic", "llm_unavailable"] = "llm"
    summary: str = ""
    relevant_folders: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    relevant_docs: list[str] = Field(default_factory=list)
    relevant_sheets: list[str] = Field(default_factory=list)
    recommended_target: str = ""
    artifact_preview_lines: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    direct_write_supported: bool = True
    executed_write_results: list[OperationalContextEntry] = Field(default_factory=list)
    media_context_supported: bool = True
    media_context_limitations: list[str] = Field(
        default_factory=lambda: [
            "Drive image/media support is metadata-only until download/OCR tools are added."
        ]
    )
    write_plan: OperationalWritePlan = Field(
        default_factory=lambda: OperationalWritePlan(target_system="google_workspace")
    )
    blockers: list[str] = Field(default_factory=list)
    approval_needs: list[str] = Field(default_factory=list)
    human_work_context: HumanWorkContext = Field(default_factory=HumanWorkContext)
    sources: list[OperationalContextSource] = Field(default_factory=list)
    diagnostics: list[OperationalContextEntry] = Field(default_factory=list)
    decision: AgentDecisionRecord = Field(
        default_factory=lambda: AgentDecisionRecord(
            decision_stage="workspace_artifact_selection",
            needs_more_context=True,
        )
    )

    @field_validator(
        "agent_name",
        "mode",
        "summary",
        "recommended_target",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "relevant_folders",
        "relevant_files",
        "relevant_docs",
        "relevant_sheets",
        "artifact_preview_lines",
        "recommended_actions",
        "media_context_limitations",
        "blockers",
        "approval_needs",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _clean_diagnostics(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @field_validator("executed_write_results", mode="before")
    @classmethod
    def _clean_executed_write_results(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @model_validator(mode="after")
    def _force_agent_name_and_boundary(self) -> GoogleWorkspaceContextResult:
        self.agent_name = "google_workspace_context_agent"
        self.write_plan.target_system = "google_workspace"
        self.write_plan.live_write_allowed_for_specialist = False
        return self


class ZoteroContextResult(BaseModel):
    """Zotero context, guarded importer results, and Chief artifact handoff plans."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str = "zotero_context_agent"
    mode: Literal["llm", "deterministic", "llm_unavailable"] = "llm"
    summary: str = ""
    library_context: str = ""
    collection_hints: list[str] = Field(default_factory=list)
    collection_keys: list[str] = Field(default_factory=list)
    article_titles: list[str] = Field(default_factory=list)
    zotero_item_keys: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    relevant_evidence: list[str] = Field(default_factory=list)
    recommended_artifact_plan: OperationalWritePlan = Field(
        default_factory=lambda: OperationalWritePlan(
            target_system="google_workspace",
            operation="create_or_update_zotero_summary_artifact",
        )
    )
    zotero_write_supported: bool = False
    zotero_test_note_write_supported: bool = True
    zotero_test_library_write_supported: bool = True
    backend_importer_supported: bool = True
    direct_workspace_write_supported: bool = True
    executed_import_results: list[OperationalContextEntry] = Field(default_factory=list)
    executed_note_results: list[OperationalContextEntry] = Field(default_factory=list)
    executed_library_results: list[OperationalContextEntry] = Field(default_factory=list)
    executed_workspace_write_results: list[OperationalContextEntry] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    approval_needs: list[str] = Field(default_factory=list)
    human_work_context: HumanWorkContext = Field(default_factory=HumanWorkContext)
    sources: list[OperationalContextSource] = Field(default_factory=list)
    diagnostics: list[OperationalContextEntry] = Field(default_factory=list)
    decision: AgentDecisionRecord = Field(
        default_factory=lambda: AgentDecisionRecord(
            decision_stage="zotero_item_selection",
            needs_more_context=True,
        )
    )

    @field_validator("agent_name", "mode", "summary", "library_context", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "collection_hints",
        "collection_keys",
        "article_titles",
        "zotero_item_keys",
        "source_ids",
        "relevant_evidence",
        "recommended_actions",
        "blockers",
        "approval_needs",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _clean_diagnostics(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @field_validator(
        "executed_import_results",
        "executed_note_results",
        "executed_library_results",
        "executed_workspace_write_results",
        mode="before",
    )
    @classmethod
    def _clean_executed_results(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)

    @model_validator(mode="after")
    def _force_agent_name_and_boundary(self) -> ZoteroContextResult:
        self.agent_name = "zotero_context_agent"
        self.zotero_write_supported = False
        self.zotero_test_note_write_supported = True
        self.zotero_test_library_write_supported = True
        self.recommended_artifact_plan.target_system = "google_workspace"
        self.recommended_artifact_plan.live_write_allowed_for_specialist = False
        return self


class HistoricalFeedContextItem(BaseModel):
    """One bounded RSS/preprint history item returned by a context specialist."""

    model_config = ConfigDict(extra="forbid")

    feed_item_id: str = ""
    title: str = ""
    url: str = ""
    source: str = ""
    feed: str = ""
    published_at: str = ""
    tags: list[str] = Field(default_factory=list)
    selected: bool = False
    relevance_status: str = ""
    selection_reason: str = ""
    summary: str = ""
    detailed_summary: str = ""
    source_basis: str = ""
    key_findings: list[str] = Field(default_factory=list)
    methods_or_design: str = ""
    limitations: list[str] = Field(default_factory=list)
    relevance_to_psychiatry: str = ""
    relevance_to_keystone: str = ""
    frontier_signal: str = ""
    evidence_status: str = ""
    publication_ids: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)
    slack_link: str = ""

    @field_validator(
        "feed_item_id",
        "title",
        "url",
        "source",
        "feed",
        "published_at",
        "relevance_status",
        "selection_reason",
        "summary",
        "detailed_summary",
        "source_basis",
        "methods_or_design",
        "relevance_to_psychiatry",
        "relevance_to_keystone",
        "frontier_signal",
        "evidence_status",
        "slack_link",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value, max_chars=900)

    @field_validator(
        "tags",
        "key_findings",
        "limitations",
        "publication_ids",
        "evidence_notes",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)


class SignalRelevanceDecisionRecord(AgentDecisionRecord):
    """Signal selection with stage identity fixed by the owning output contract."""

    decision_stage: Literal["signal_relevance_selection"] = (
        "signal_relevance_selection"
    )


class HistoricalFeedContextResult(BaseModel):
    """Historical RSS/preprint context and Chief of Staff handoff guidance."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str = "historical_feed_context_agent"
    mode: Literal["llm", "deterministic", "llm_unavailable"] = "llm"
    summary: str = ""
    query: str = ""
    source_focus: str = ""
    retrieved_item_ids: list[str] = Field(default_factory=list)
    articles: list[HistoricalFeedContextItem] = Field(default_factory=list)
    frontier_summary: str = ""
    recurring_themes: list[str] = Field(default_factory=list)
    research_frontiers: list[str] = Field(default_factory=list)
    clinical_translation_signals: list[str] = Field(default_factory=list)
    market_or_partnership_signals: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    opportunity_signals: list[str] = Field(default_factory=list)
    future_directions: list[str] = Field(default_factory=list)
    monitoring_queries: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    approval_needs: list[str] = Field(default_factory=list)
    human_work_context: HumanWorkContext = Field(default_factory=HumanWorkContext)
    sources: list[OperationalContextSource] = Field(default_factory=list)
    diagnostics: list[OperationalContextEntry] = Field(default_factory=list)
    decision: SignalRelevanceDecisionRecord = Field(
        default_factory=lambda: SignalRelevanceDecisionRecord(
            needs_more_context=True,
        )
    )

    @field_validator(
        "agent_name",
        "mode",
        "summary",
        "query",
        "source_focus",
        "frontier_summary",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator(
        "retrieved_item_ids",
        "recurring_themes",
        "research_frontiers",
        "clinical_translation_signals",
        "market_or_partnership_signals",
        "evidence_gaps",
        "opportunity_signals",
        "future_directions",
        "monitoring_queries",
        "recommended_actions",
        "blockers",
        "approval_needs",
        mode="before",
    )
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)

    @field_validator("decision", mode="before")
    @classmethod
    def _validate_signal_decision_contract(cls, value: object) -> object:
        if isinstance(value, AgentDecisionRecord):
            return value.model_dump(mode="python")
        return value

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _clean_diagnostics(cls, value: object) -> list[OperationalContextEntry]:
        return _clean_entries(value)


class RssContextResult(HistoricalFeedContextResult):
    """RSS/#announcements history context for Chief of Staff."""

    agent_name: str = "rss_context_agent"
    source_focus: str = "rss_announcements"

    @model_validator(mode="after")
    def _force_agent_name(self) -> RssContextResult:
        self.agent_name = "rss_context_agent"
        if not self.source_focus:
            self.source_focus = "rss_announcements"
        return self


class PreprintsContextResult(HistoricalFeedContextResult):
    """Preprint/#knowledge-hub history context for Chief of Staff."""

    agent_name: str = "preprints_context_agent"
    source_focus: str = "preprints"

    @model_validator(mode="after")
    def _force_agent_name(self) -> PreprintsContextResult:
        self.agent_name = "preprints_context_agent"
        if not self.source_focus:
            self.source_focus = "preprints"
        return self
