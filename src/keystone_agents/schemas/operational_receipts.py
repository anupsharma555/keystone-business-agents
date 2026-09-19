"""Read-only WorkItem receipt inspection and resume-point contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OperationalStageReceipt(BaseModel):
    """Durable evidence that one WorkItem stage produced canonical output."""

    model_config = ConfigDict(extra="forbid")

    route: str
    stage: str = ""
    status: str
    artifact_types: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    event_type: str = ""
    event_created_at: str = ""
    verification_basis: str


class OperationalToolReceipt(BaseModel):
    """Minimal verified provider-tool evidence safe to expose to a manager agent."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    operation: str
    provider: str = ""
    status: str = ""
    receipt_kind: Literal["read", "write"]
    object_id: str = ""
    verification_basis: str


class WorkItemResumePoint(BaseModel):
    """Exact deterministic resume disposition derived from canonical WorkItem state."""

    model_config = ConfigDict(extra="forbid")

    disposition: Literal[
        "resume",
        "await_approval",
        "blocked",
        "terminal",
        "indeterminate",
    ]
    exact: bool
    required: bool
    stage: str = ""
    agent: str = ""
    description: str = ""
    command_hint: str = ""
    requires_approval: bool = False
    basis: list[str] = Field(default_factory=list)
    do_not_repeat_tool_names: list[str] = Field(default_factory=list)
    do_not_recreate_object_ids: list[str] = Field(default_factory=list)
    do_not_repeat_stages: list[str] = Field(default_factory=list)


class WorkItemReceiptInspection(BaseModel):
    """Bounded read-only operational view over one persisted WorkItem."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.work_item_receipt_inspection.v1"] = (
        "keystone.work_item_receipt_inspection.v1"
    )
    inspection_status: Literal["ok", "not_found"] = "ok"
    work_item_id: str
    work_item_status: str = ""
    current_route: str = ""
    last_agent: str = ""
    stage_receipts: list[OperationalStageReceipt] = Field(default_factory=list)
    tool_receipts: list[OperationalToolReceipt] = Field(default_factory=list)
    ignored_unverified_receipt_count: int = Field(default=0, ge=0)
    open_blocker_codes: list[str] = Field(default_factory=list)
    unresolved_approval_scopes: list[str] = Field(default_factory=list)
    resume_point: WorkItemResumePoint
    canonical_state: Literal["sqlite_work_item"] = "sqlite_work_item"
    read_only: Literal[True] = True
    provider_calls_performed: Literal[0] = 0
    external_write_performed: Literal[False] = False


__all__ = [
    "OperationalStageReceipt",
    "OperationalToolReceipt",
    "WorkItemReceiptInspection",
    "WorkItemResumePoint",
]
