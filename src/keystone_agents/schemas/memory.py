"""Typed local memory records for Keystone agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from keystone_agents.schemas.approval import ApprovalState, normalize_approval_state

MemoryType = Literal[
    "company_profile_snapshot",
    "company_fact",
    "opportunity_signal",
    "opportunity_outcome",
    "email_style_preference",
    "outreach_example",
    "human_feedback",
    "approval_decision",
    "blocked_fact",
    "risk_flag",
    "retrieval_tool_performance",
    "workflow_dedup",
]
MemoryObjectType = Literal[
    "company",
    "company_profile",
    "opportunity",
    "opportunity_scout",
    "outreach_draft",
    "email_style_profile",
    "feedback",
    "approval",
    "workflow",
    "other",
]
MemorySensitivity = Literal["public", "internal", "sensitive", "secret", "phi"]

APPROVED_MEMORY_STATES = {
    ApprovalState.APPROVED_FOR_RESEARCH,
    ApprovalState.APPROVED_FOR_DRAFTING,
    ApprovalState.APPROVED_FOR_EXTERNAL_USE,
    ApprovalState.APPROVED_FOR_SEND,
}


def memory_timestamp() -> str:
    """Return an ISO UTC timestamp for local memory records."""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_memory_key(value: str) -> str:
    """Normalize an object key for deterministic local lookup."""

    return " ".join(value.lower().replace("_", " ").replace("-", " ").split()).strip()


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u2014", "-").split()).strip()


def _safe_json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_safe_json_text(item) for item in value.values())
    if isinstance(value, list | tuple | set):
        return " ".join(_safe_json_text(item) for item in value)
    return _clean(value)


class MemoryItem(BaseModel):
    """One durable, source-aware local memory item.

    Memory is data, not hidden prompt mutation. Items default to pending and become reusable
    only after approval and safety checks.
    """

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    memory_type: MemoryType
    object_type: MemoryObjectType = "other"
    object_id: str = ""
    object_key: str = ""
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    content: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    approval_state: ApprovalState = ApprovalState.PENDING
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    sensitivity: MemorySensitivity = "internal"
    safe_for_prompt: bool = True
    created_at: str = Field(default_factory=memory_timestamp)
    expires_at: str | None = None
    supersedes_memory_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw_email_body_included: bool = False
    secrets_included: bool = False
    phi_included: bool = False
    send_enabled: bool = False
    sent: bool = False

    @field_validator("approval_state", mode="before")
    @classmethod
    def _normalize_approval_state(cls, value: Any) -> ApprovalState:
        return normalize_approval_state(value)

    @field_validator("object_id", "object_key", "title", "summary", mode="before")
    @classmethod
    def _clean_text(cls, value: Any) -> str:
        return _clean(value)

    @field_validator("source_ids", mode="before")
    @classmethod
    def _clean_source_ids(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise TypeError("source_ids must be a list")
        return list(dict.fromkeys(_clean(item) for item in value if _clean(item)))

    @field_validator(
        "raw_email_body_included",
        "secrets_included",
        "phi_included",
        "send_enabled",
        "sent",
    )
    @classmethod
    def _unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError(
                "memory items must not store raw bodies, secrets, PHI, or sending state"
            )
        return value

    @model_validator(mode="after")
    def _validate_memory(self) -> MemoryItem:
        if not self.object_key:
            self.object_key = normalize_memory_key(self.object_id or self.title)
        else:
            self.object_key = normalize_memory_key(self.object_key)
        if self.sensitivity in {"secret", "phi"}:
            self.safe_for_prompt = False
        if self.safe_for_prompt and self.sensitivity in {"secret", "phi"}:
            raise ValueError("secret or PHI memory cannot be prompt-safe")
        return self

    @property
    def approved_for_reuse(self) -> bool:
        return self.approval_state in APPROVED_MEMORY_STATES and self.safe_for_prompt

    def index_text(self) -> str:
        """Return safe searchable text for local indexing."""

        return _clean(
            " ".join(
                [
                    self.title,
                    self.summary,
                    _safe_json_text(self.content),
                    " ".join(self.source_ids),
                ]
            )
        )

    def prompt_context(self) -> dict[str, Any]:
        """Return the safe context shape used by retrieval tools."""

        return {
            "id": self.id,
            "memory_type": self.memory_type,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "object_key": self.object_key,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "source_ids": self.source_ids,
            "approval_state": self.approval_state.value,
            "confidence": self.confidence,
            "sensitivity": self.sensitivity,
            "safe_for_prompt": self.safe_for_prompt,
            "created_at": self.created_at,
        }


class MemoryRetrievalResult(BaseModel):
    """Structured result envelope for local memory retrieval."""

    query: str = ""
    object_key: str = ""
    approved_only: bool = True
    records: list[MemoryItem] = Field(default_factory=list)
    send_enabled: bool = False

    @field_validator("send_enabled")
    @classmethod
    def _send_enabled_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("memory retrieval must not enable sending")
        return value
