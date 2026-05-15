"""Automation and Chief of Staff operating-layer schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def automation_timestamp() -> str:
    """Return a compact UTC timestamp for automation records."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_automation_id(prefix: str) -> str:
    """Return a stable opaque id with a readable prefix."""

    return f"{prefix}_{uuid4().hex}"


def _json_text(value: object) -> str:
    """Represent flexible metadata in a strict-schema-safe string field."""

    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _json_text_list(value: object) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    return [text for item in values if (text := _json_text(item))]


class AutomationTriggerType(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    SLACK_ACTION = "slack_action"
    GMAIL_QUERY = "gmail_query"


class AutomationStatus(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    DISABLED = "disabled"


class AutomationRunStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"


class AutomationFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class AutomationWriteDestination(StrEnum):
    LOCAL_MARKDOWN = "local_markdown"
    LOCAL_JSON = "local_json"
    GOOGLE_DOC = "google_doc"
    AIRTABLE = "airtable"
    SLACK = "slack"
    WORK_ITEM_NOTE = "work_item_note"
    AUTOMATION_RUN_NOTE = "automation_run_note"


class AutomationSpec(BaseModel):
    """Configured scheduled or operator-triggered automation."""

    id: str = Field(default_factory=lambda: new_automation_id("auto"))
    name: str
    description: str = ""
    trigger_type: AutomationTriggerType = AutomationTriggerType.MANUAL
    schedule: str = ""
    target_agent: str = ""
    workflow: str = ""
    input_template: str = ""
    status: AutomationStatus = AutomationStatus.ENABLED
    approval_policy: str = "approval_required_for_external_writes"
    live_flags: list[str] = Field(default_factory=list)
    default_channel: str = ""
    metadata: str = ""
    created_at: str = Field(default_factory=automation_timestamp)
    updated_at: str = Field(default_factory=automation_timestamp)

    @field_validator("name")
    @classmethod
    def _name_required(cls, value: str) -> str:
        cleaned = " ".join(str(value or "").strip().split())
        if not cleaned:
            raise ValueError("automation name is required")
        return cleaned

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_json(cls, value: object) -> str:
        return _json_text(value)


class AutomationChannelBinding(BaseModel):
    """Slack channel or review destination tied to an automation."""

    id: str = Field(default_factory=lambda: new_automation_id("acb"))
    automation_id: str
    channel_id: str = ""
    channel_name: str = ""
    destination_type: str = "slack"
    purpose: str = "review"
    approval_required: bool = True
    metadata: str = ""
    created_at: str = Field(default_factory=automation_timestamp)

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_json(cls, value: object) -> str:
        return _json_text(value)


class AutomationRun(BaseModel):
    """One execution record for an automation."""

    id: str = Field(default_factory=lambda: new_automation_id("arun"))
    automation_id: str
    automation_name: str = ""
    stage: str = ""
    status: AutomationRunStatus = AutomationRunStatus.DRY_RUN
    work_item_id: str = ""
    channel: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    approval_count: int = 0
    failure_summary: str = ""
    next_safe_action: str = ""
    command: list[str] = Field(default_factory=list)
    output_summary: str = ""
    metadata: str = ""
    started_at: str = Field(default_factory=automation_timestamp)
    completed_at: str = Field(default_factory=automation_timestamp)

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def _artifact_refs_json(cls, value: object) -> list[str]:
        return _json_text_list(value)

    @field_validator("output_summary", "metadata", mode="before")
    @classmethod
    def _json_fields(cls, value: object) -> str:
        return _json_text(value)


class AutomationFinding(BaseModel):
    """Reviewable finding from an automation or channel audit."""

    id: str = Field(default_factory=lambda: new_automation_id("afind"))
    automation_id: str = ""
    run_id: str = ""
    channel: str = ""
    finding_type: str = "status"
    severity: AutomationFindingSeverity = AutomationFindingSeverity.INFO
    title: str
    summary: str = ""
    recommendation: str = ""
    source_refs: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=automation_timestamp)

    @field_validator("title")
    @classmethod
    def _title_required(cls, value: str) -> str:
        cleaned = " ".join(str(value or "").strip().split())
        if not cleaned:
            raise ValueError("finding title is required")
        return cleaned

    @field_validator("source_refs", mode="before")
    @classmethod
    def _source_refs_json(cls, value: object) -> list[str]:
        return _json_text_list(value)


class AutomationArtifactRef(BaseModel):
    """Published or local artifact created by the operating layer."""

    artifact_id: str = Field(default_factory=lambda: new_automation_id("aart"))
    artifact_type: str
    title: str = ""
    url: str = ""
    path: str = ""
    provider: str = "local"
    dry_run: bool = True
    metadata: str = ""
    created_at: str = Field(default_factory=automation_timestamp)

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_json(cls, value: object) -> str:
        return _json_text(value)


class ChiefOfStaffWriteRequest(BaseModel):
    """One bounded write requested or completed by Chief of Staff."""

    destination: AutomationWriteDestination
    title: str = ""
    summary: str = ""
    approval_required: bool = True
    live_required: bool = False
    allowed: bool = False
    status: str = "planned"
    artifact_ref: AutomationArtifactRef | None = None
    blocked_reason: str = ""
    metadata: str = ""

    @field_validator("metadata", mode="before")
    @classmethod
    def _metadata_json(cls, value: object) -> str:
        return _json_text(value)


class AutomationInventoryReport(BaseModel):
    """Chief of Staff report over current automations and review channels."""

    report_id: str = Field(default_factory=lambda: new_automation_id("airep"))
    title: str = "Chief of Staff Automation Inventory"
    summary: str = ""
    generated_at: str = Field(default_factory=automation_timestamp)
    channels_reviewed: list[str] = Field(default_factory=list)
    automation_specs: list[AutomationSpec] = Field(default_factory=list)
    recent_runs: list[AutomationRun] = Field(default_factory=list)
    channel_bindings: list[AutomationChannelBinding] = Field(default_factory=list)
    findings: list[AutomationFinding] = Field(default_factory=list)
    pending_approval_count: int = 0
    recommended_actions: list[str] = Field(default_factory=list)
    blocked_actions: list[str] = Field(default_factory=list)
    write_requests: list[ChiefOfStaffWriteRequest] = Field(default_factory=list)
    artifact_refs: list[AutomationArtifactRef] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)


class NaturalInteractionResolution(BaseModel):
    """Resolved meaning of a natural Slack or CLI follow-up."""

    input_text: str
    intent: str = "clarification"
    target_type: str = ""
    target_id: str = ""
    route: str = "chief_of_staff"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    explanation: str = ""
    requires_clarification: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
