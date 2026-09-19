"""Typed trigger, deduplication, and checkpoint state for feed-signal agents."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SIGNAL_LIFECYCLE_SCHEMA = "keystone.signal_lifecycle_checkpoint.v1"
SIGNAL_LIFECYCLE_ARTIFACT_TYPE = "signal_lifecycle_checkpoint"


class SignalSourceKind(StrEnum):
    """Supported read-only signal families."""

    RSS = "rss"
    PREPRINTS = "preprints"


class SignalLifecycleStage(StrEnum):
    """Ordered stages that may be checkpointed and resumed."""

    CONTEXT_RETRIEVED = "context_retrieved"
    HANDOFF_PREPARED = "handoff_prepared"
    REVIEW_COMPLETED = "review_completed"
    COMPLETED = "completed"


SIGNAL_LIFECYCLE_STAGE_ORDER: tuple[SignalLifecycleStage, ...] = (
    SignalLifecycleStage.CONTEXT_RETRIEVED,
    SignalLifecycleStage.HANDOFF_PREPARED,
    SignalLifecycleStage.REVIEW_COMPLETED,
    SignalLifecycleStage.COMPLETED,
)


class SignalDisposition(StrEnum):
    """Admission result for one canonical source identity."""

    ADMITTED = "admitted"
    UPDATED = "updated"
    DUPLICATE = "duplicate"
    INVALID = "invalid"


class SignalLifecycleStatus(StrEnum):
    """Current durable lifecycle status."""

    PLANNED = "planned"
    READY = "ready"
    PARTIAL_FAILURE = "partial_failure"
    DUPLICATE_TRIGGER = "duplicate_trigger"
    NO_ACTION = "no_action"
    COMPLETED = "completed"


class SignalTriggerContext(BaseModel):
    """Stable trigger identity supplied to a WorkItem or context pack."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.signal_trigger.v1"] = "keystone.signal_trigger.v1"
    source_kind: SignalSourceKind
    trigger_ref: str = Field(min_length=1, max_length=500)
    dedupe_scope: str = Field(min_length=1, max_length=300)
    trigger_label: str = Field(default="", max_length=160)
    scheduled: bool = False

    @field_validator("trigger_ref", "dedupe_scope", "trigger_label", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").split()).strip()


class SignalIdentityDecision(BaseModel):
    """Bounded identity and admission evidence for one source item."""

    model_config = ConfigDict(extra="forbid")

    item_ref: str = ""
    canonical_identity: str = ""
    revision_identity: str = ""
    disposition: SignalDisposition = SignalDisposition.INVALID
    matched_work_item_id: str = ""
    reason: str = ""


class SignalLifecycleCheckpoint(BaseModel):
    """Durable, WorkItem-owned state for one RSS or preprint trigger."""

    model_config = ConfigDict(extra="forbid")

    schema_name: Literal["keystone.signal_lifecycle_checkpoint.v1"] = (
        SIGNAL_LIFECYCLE_SCHEMA
    )
    work_item_id: str
    source_kind: SignalSourceKind
    trigger_id: str
    trigger_ref_digest: str
    dedupe_scope_digest: str
    trigger_label: str = ""
    scheduled: bool = False
    status: SignalLifecycleStatus = SignalLifecycleStatus.READY
    completed_stages: list[SignalLifecycleStage] = Field(default_factory=list)
    next_stage: SignalLifecycleStage | None = SignalLifecycleStage.CONTEXT_RETRIEVED
    decisions: list[SignalIdentityDecision] = Field(default_factory=list, max_length=25)
    admitted_revision_ids: list[str] = Field(default_factory=list, max_length=25)
    duplicate_revision_ids: list[str] = Field(default_factory=list, max_length=25)
    canonical_work_item_id: str = ""
    canonical_work_item_ids: list[str] = Field(default_factory=list, max_length=25)
    resumed: bool = False
    attempt_count: int = Field(default=1, ge=1, le=100)
    failed_stage: str = ""
    failure_code: str = ""
    failure_summary: str = ""
    safe_next_action: str = ""
    dry_run: bool = True
    provider_calls_made: int = Field(default=0, ge=0)
    external_writes_performed: bool = False
    admission_guard: str = Field(default="", max_length=80)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


__all__ = [
    "SIGNAL_LIFECYCLE_ARTIFACT_TYPE",
    "SIGNAL_LIFECYCLE_SCHEMA",
    "SIGNAL_LIFECYCLE_STAGE_ORDER",
    "SignalDisposition",
    "SignalIdentityDecision",
    "SignalLifecycleCheckpoint",
    "SignalLifecycleStage",
    "SignalLifecycleStatus",
    "SignalSourceKind",
    "SignalTriggerContext",
]
