"""Approval state machine and request schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApprovalState(StrEnum):
    """Explicit approval decisions for gated Keystone workflow actions."""

    PENDING = "pending"
    APPROVED_FOR_RESEARCH = "approved_for_research"
    APPROVED_FOR_DRAFTING = "approved_for_drafting"
    APPROVED_FOR_EXTERNAL_USE = "approved_for_external_use"
    APPROVED_FOR_SEND = "approved_for_send"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalScope(StrEnum):
    """Workflow action an approval decision applies to."""

    RESEARCH = "research"
    DRAFTING = "drafting"
    EXTERNAL_USE = "external_use"
    SEND = "send"


class ApprovalQueueObjectType(StrEnum):
    """Objects that can wait in the human approval queue."""

    GMAIL_DRAFT = "gmail_draft"
    OUTREACH_DRAFT = "outreach_draft"
    OPPORTUNITY = "opportunity"
    COMPANY_PROFILE = "company_profile"
    OTHER = "other"


class ApprovalQueueStatus(StrEnum):
    """Human review status for approval queue items."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVISE = "revise"
    ARCHIVED = "archived"
    EXPIRED = "expired"


class ApprovalTransitionError(ValueError):
    """Raised when an approval transition skips or reopens a terminal state."""


VALID_APPROVAL_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.PENDING: frozenset(
        {
            ApprovalState.PENDING,
            ApprovalState.APPROVED_FOR_RESEARCH,
            ApprovalState.APPROVED_FOR_DRAFTING,
            ApprovalState.APPROVED_FOR_EXTERNAL_USE,
            ApprovalState.APPROVED_FOR_SEND,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED_FOR_RESEARCH: frozenset(
        {
            ApprovalState.APPROVED_FOR_RESEARCH,
            ApprovalState.APPROVED_FOR_DRAFTING,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED_FOR_DRAFTING: frozenset(
        {
            ApprovalState.APPROVED_FOR_DRAFTING,
            ApprovalState.APPROVED_FOR_EXTERNAL_USE,
            ApprovalState.APPROVED_FOR_SEND,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED_FOR_EXTERNAL_USE: frozenset(
        {
            ApprovalState.APPROVED_FOR_EXTERNAL_USE,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED_FOR_SEND: frozenset(
        {
            ApprovalState.APPROVED_FOR_SEND,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.REJECTED: frozenset({ApprovalState.REJECTED}),
    ApprovalState.EXPIRED: frozenset({ApprovalState.EXPIRED}),
}

VALID_APPROVAL_QUEUE_TRANSITIONS: dict[ApprovalQueueStatus, frozenset[ApprovalQueueStatus]] = {
    ApprovalQueueStatus.PENDING: frozenset(
        {
            ApprovalQueueStatus.PENDING,
            ApprovalQueueStatus.APPROVED,
            ApprovalQueueStatus.REJECTED,
            ApprovalQueueStatus.REVISE,
            ApprovalQueueStatus.ARCHIVED,
            ApprovalQueueStatus.EXPIRED,
        }
    ),
    ApprovalQueueStatus.REVISE: frozenset(
        {
            ApprovalQueueStatus.PENDING,
            ApprovalQueueStatus.REVISE,
            ApprovalQueueStatus.ARCHIVED,
            ApprovalQueueStatus.EXPIRED,
        }
    ),
    ApprovalQueueStatus.APPROVED: frozenset(
        {
            ApprovalQueueStatus.APPROVED,
            ApprovalQueueStatus.ARCHIVED,
            ApprovalQueueStatus.EXPIRED,
        }
    ),
    ApprovalQueueStatus.REJECTED: frozenset(
        {
            ApprovalQueueStatus.REJECTED,
            ApprovalQueueStatus.ARCHIVED,
        }
    ),
    ApprovalQueueStatus.EXPIRED: frozenset(
        {
            ApprovalQueueStatus.EXPIRED,
            ApprovalQueueStatus.ARCHIVED,
        }
    ),
    ApprovalQueueStatus.ARCHIVED: frozenset({ApprovalQueueStatus.ARCHIVED}),
}

APPROVED_STATE_BY_SCOPE: dict[ApprovalScope, ApprovalState] = {
    ApprovalScope.RESEARCH: ApprovalState.APPROVED_FOR_RESEARCH,
    ApprovalScope.DRAFTING: ApprovalState.APPROVED_FOR_DRAFTING,
    ApprovalScope.EXTERNAL_USE: ApprovalState.APPROVED_FOR_EXTERNAL_USE,
    ApprovalScope.SEND: ApprovalState.APPROVED_FOR_SEND,
}

SCOPE_BY_APPROVED_STATE: dict[ApprovalState, ApprovalScope] = {
    state: scope for scope, state in APPROVED_STATE_BY_SCOPE.items()
}


def approval_timestamp() -> str:
    """Return a compact UTC timestamp for persisted approval decisions."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def approval_queue_timestamp() -> datetime:
    """Return a timezone-aware UTC timestamp for queue records."""

    return datetime.now(UTC).replace(microsecond=0)


def _approval_queue_id() -> str:
    return f"approval_{uuid4().hex}"


def normalize_approval_state(value: ApprovalState | str | None) -> ApprovalState:
    """Normalize an approval state string into the explicit enum."""

    if value is None or value == "":
        return ApprovalState.PENDING
    if isinstance(value, ApprovalState):
        return value
    aliases = {
        "approved_for_use": ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        "approved_for_external": ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        "external_use_approved": ApprovalState.APPROVED_FOR_EXTERNAL_USE,
    }
    normalized = str(value).strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ApprovalState(normalized)
    except ValueError as exc:
        valid = ", ".join(state.value for state in ApprovalState)
        raise ValueError(f"approval state must be one of: {valid}") from exc


def normalize_approval_scope(value: ApprovalScope | str | None) -> ApprovalScope:
    """Normalize an approval scope string into the explicit enum."""

    if value is None or value == "":
        return ApprovalScope.SEND
    if isinstance(value, ApprovalScope):
        return value
    aliases = {
        "external": ApprovalScope.EXTERNAL_USE,
        "use": ApprovalScope.EXTERNAL_USE,
        "draft_external_use": ApprovalScope.EXTERNAL_USE,
    }
    normalized = str(value).strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ApprovalScope(normalized)
    except ValueError as exc:
        valid = ", ".join(scope.value for scope in ApprovalScope)
        raise ValueError(f"approval scope must be one of: {valid}") from exc


def normalize_approval_queue_object_type(
    value: ApprovalQueueObjectType | str | None,
) -> ApprovalQueueObjectType:
    """Normalize a queue object type string into the explicit enum."""

    if value is None or value == "":
        return ApprovalQueueObjectType.OTHER
    if isinstance(value, ApprovalQueueObjectType):
        return value
    aliases = {
        "company": ApprovalQueueObjectType.COMPANY_PROFILE,
        "opportunity_record": ApprovalQueueObjectType.OPPORTUNITY,
        "gmail_reply": ApprovalQueueObjectType.GMAIL_DRAFT,
        "gmail_draft_recommendation": ApprovalQueueObjectType.GMAIL_DRAFT,
        "draft": ApprovalQueueObjectType.OTHER,
    }
    normalized = str(value).strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return ApprovalQueueObjectType(normalized)
    except ValueError as exc:
        valid = ", ".join(object_type.value for object_type in ApprovalQueueObjectType)
        raise ValueError(f"approval queue object type must be one of: {valid}") from exc


def normalize_approval_queue_status(
    value: ApprovalQueueStatus | str | None,
) -> ApprovalQueueStatus:
    """Normalize a queue status string into the explicit enum."""

    if value is None or value == "":
        return ApprovalQueueStatus.PENDING
    if isinstance(value, ApprovalQueueStatus):
        return value
    try:
        return ApprovalQueueStatus(str(value).strip().lower())
    except ValueError as exc:
        valid = ", ".join(status.value for status in ApprovalQueueStatus)
        raise ValueError(f"approval queue status must be one of: {valid}") from exc


def approved_state_for_scope(scope: ApprovalScope | str) -> ApprovalState:
    """Return the only approval state that grants a specific workflow scope."""

    return APPROVED_STATE_BY_SCOPE[normalize_approval_scope(scope)]


def validate_approval_transition(
    current: ApprovalState | str | None,
    next_state: ApprovalState | str,
) -> ApprovalState:
    """Validate and return the next approval state."""

    resolved_current = normalize_approval_state(current)
    resolved_next = normalize_approval_state(next_state)
    if resolved_next not in VALID_APPROVAL_TRANSITIONS[resolved_current]:
        raise ApprovalTransitionError(
            f"invalid approval transition: {resolved_current.value} -> {resolved_next.value}"
        )
    return resolved_next


def validate_approval_queue_transition(
    current: ApprovalQueueStatus | str | None,
    next_status: ApprovalQueueStatus | str,
) -> ApprovalQueueStatus:
    """Validate and return the next approval queue status."""

    resolved_current = normalize_approval_queue_status(current)
    resolved_next = normalize_approval_queue_status(next_status)
    if resolved_next not in VALID_APPROVAL_QUEUE_TRANSITIONS[resolved_current]:
        raise ApprovalTransitionError(
            f"invalid approval queue transition: {resolved_current.value} -> {resolved_next.value}"
        )
    return resolved_next


def approval_decision_matches_scope(
    decision: ApprovalState | str,
    scope: ApprovalScope | str,
) -> bool:
    """Return whether an approval decision is valid for the requested scope."""

    resolved_decision = normalize_approval_state(decision)
    resolved_scope = normalize_approval_scope(scope)
    if resolved_decision in {
        ApprovalState.PENDING,
        ApprovalState.REJECTED,
        ApprovalState.EXPIRED,
    }:
        return True
    if resolved_scope in {ApprovalScope.EXTERNAL_USE, ApprovalScope.SEND}:
        return resolved_decision in {
            ApprovalState.APPROVED_FOR_EXTERNAL_USE,
            ApprovalState.APPROVED_FOR_SEND,
        }
    return SCOPE_BY_APPROVED_STATE[resolved_decision] == resolved_scope


def state_allows_research(state: ApprovalState | str | None) -> bool:
    return normalize_approval_state(state) == ApprovalState.APPROVED_FOR_RESEARCH


def state_allows_drafting(state: ApprovalState | str | None) -> bool:
    return normalize_approval_state(state) == ApprovalState.APPROVED_FOR_DRAFTING


def state_allows_external_use(state: ApprovalState | str | None) -> bool:
    return normalize_approval_state(state) in {
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }


def state_allows_sending(state: ApprovalState | str | None) -> bool:
    normalize_approval_state(state)
    return False


def external_use_state_from_legacy(state: ApprovalState | str | None) -> ApprovalState:
    """Normalize old send approval wording into the external-use checkpoint."""

    resolved = normalize_approval_state(state)
    if resolved == ApprovalState.APPROVED_FOR_SEND:
        return ApprovalState.APPROVED_FOR_EXTERNAL_USE
    return resolved


def approval_queue_status_allows_sending(
    status: ApprovalQueueStatus | str | None,
) -> bool:
    """Queue review status never enables external sending by itself."""

    normalize_approval_queue_status(status)
    return False


def queue_status_for_decision(decision: ApprovalState | str | None) -> ApprovalQueueStatus:
    """Map legacy approval decisions to local queue review statuses."""

    resolved = normalize_approval_state(decision)
    if resolved == ApprovalState.PENDING:
        return ApprovalQueueStatus.PENDING
    if resolved == ApprovalState.REJECTED:
        return ApprovalQueueStatus.REJECTED
    if resolved == ApprovalState.EXPIRED:
        return ApprovalQueueStatus.EXPIRED
    return ApprovalQueueStatus.APPROVED


class ApprovalCheckpoint(BaseModel):
    """Explicit human-control checkpoint for a workflow stage."""

    model_config = ConfigDict(extra="forbid")

    scope: ApprovalScope
    state: ApprovalState = ApprovalState.PENDING
    required: bool = True
    approved: bool = False
    rationale: str = ""
    send_enabled: bool = False

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @field_validator("state", mode="before")
    @classmethod
    def normalize_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("send_enabled")
    @classmethod
    def reject_send_enabled(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("approval checkpoints must not enable sending")
        return value


class ApprovalDecisionRecord(BaseModel):
    """Persisted human approval decision."""

    model_config = ConfigDict(extra="forbid")

    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    decision: ApprovalState = ApprovalState.PENDING
    scope: ApprovalScope = ApprovalScope.SEND
    reviewer: str = ""
    timestamp: str = Field(default_factory=approval_timestamp)
    notes: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    source_agent: str = ""

    @field_validator("object_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: Any) -> str:
        if value is None or value == "":
            raise ValueError("object_id is required for approval decisions.")
        return str(value)

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @field_validator("risk_flags", mode="before")
    @classmethod
    def normalize_risk_flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_decision_scope(self) -> ApprovalDecisionRecord:
        if not approval_decision_matches_scope(self.decision, self.scope):
            raise ValueError(
                f"{self.decision.value} is not valid for {self.scope.value} approval scope."
            )
        return self


class ApprovalQueueItem(BaseModel):
    """Persisted artifact awaiting human review."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_approval_queue_id, min_length=1)
    object_type: ApprovalQueueObjectType = ApprovalQueueObjectType.OTHER
    object_id: str | None = None
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    draft_text: str | None = None
    source_agent: str = Field(min_length=1)
    risk_flags: list[str] = Field(default_factory=list)
    approval_status: ApprovalQueueStatus = ApprovalQueueStatus.PENDING
    reviewer: str | None = None
    reviewer_notes: str | None = None
    created_at: datetime = Field(default_factory=approval_queue_timestamp)
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("object_type", mode="before")
    @classmethod
    def normalize_object_type(cls, value: Any) -> ApprovalQueueObjectType:
        return normalize_approval_queue_object_type(value)

    @field_validator("approval_status", mode="before")
    @classmethod
    def normalize_status(cls, value: Any) -> ApprovalQueueStatus:
        return normalize_approval_queue_status(value)

    @field_validator("object_id", mode="before")
    @classmethod
    def stringify_optional_object_id(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    @field_validator("risk_flags", mode="before")
    @classmethod
    def normalize_risk_flags(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return [str(item) for item in value if str(item).strip()]

    @field_validator("created_at", "updated_at", "expires_at", mode="before")
    @classmethod
    def parse_datetime(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC, microsecond=0)
            return value.astimezone(UTC).replace(microsecond=0)
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).replace(microsecond=0)

    @field_validator("title", "summary", "draft_text")
    @classmethod
    def reject_em_dash(cls, value: str | None) -> str | None:
        if value is not None and "\u2014" in value:
            raise ValueError("Approval queue text must not contain em dashes.")
        return value


class ApprovalRequest(BaseModel):
    """Human approval request for draft-only outbound work."""

    model_config = ConfigDict(extra="forbid")

    object_type: str = Field(min_length=1)
    object_id: str | None = None
    summary: str = Field(min_length=1)
    draft_text: str = ""
    risk_flags: list[str] = Field(default_factory=list)
    decision: ApprovalState = ApprovalState.PENDING
    scope: ApprovalScope = ApprovalScope.EXTERNAL_USE
    reviewer: str = ""
    timestamp: str = Field(default_factory=approval_timestamp)
    notes: str = ""
    slack_ts: str | None = None

    @field_validator("object_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return str(value)

    @field_validator("summary", "draft_text")
    @classmethod
    def reject_em_dash(cls, value: str) -> str:
        if "\u2014" in value:
            raise ValueError("Approval request text must not contain em dashes.")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def normalize_decision(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> ApprovalScope:
        return normalize_approval_scope(value)

    @model_validator(mode="after")
    def validate_decision_scope(self) -> ApprovalRequest:
        if not approval_decision_matches_scope(self.decision, self.scope):
            raise ValueError(
                f"{self.decision.value} is not valid for {self.scope.value} approval scope."
            )
        return self

    @property
    def approval_state(self) -> ApprovalState:
        return self.decision

    @property
    def approval_status(self) -> str:
        return self.decision.value
