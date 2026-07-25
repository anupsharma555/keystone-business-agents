"""Typed provider evidence for Chief-owned multi-source context summaries."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ChiefContextSource = Literal["gmail", "airtable", "work_items"]
ChiefContextReceiptStatus = Literal["success", "empty", "blocked"]


def _compact_text(value: object, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


class ChiefContextEvidenceItem(BaseModel):
    """One bounded provider or local-state item supplied to the Chief."""

    model_config = ConfigDict(extra="forbid")

    source: ChiefContextSource
    source_id: str
    title: str = ""
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_id", "title", "summary", mode="before")
    @classmethod
    def _clean_text_fields(cls, value: object) -> str:
        return _compact_text(value)


class ChiefContextEvidenceReceipt(BaseModel):
    """Verifiable outcome of one bounded read obligation."""

    model_config = ConfigDict(extra="forbid")

    source: ChiefContextSource
    provider: str
    operation: str
    status: ChiefContextReceiptStatus
    verified: bool = False
    provider_read: bool = False
    item_count: int = Field(default=0, ge=0)
    query: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    details: str = ""
    error_type: str = ""

    @field_validator(
        "provider",
        "operation",
        "query",
        "details",
        "error_type",
        mode="before",
    )
    @classmethod
    def _clean_text_fields(cls, value: object) -> str:
        return _compact_text(value)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _clean_evidence_ids(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                _compact_text(item, max_chars=160)
                for item in values
                if _compact_text(item, max_chars=160)
            )
        )[:50]


class ChiefContextEvidenceBundle(BaseModel):
    """Complete bounded context acquired before one Chief synthesis call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "keystone.chief_context_evidence.v1"
    required_sources: list[ChiefContextSource] = Field(default_factory=list)
    receipts: list[ChiefContextEvidenceReceipt] = Field(default_factory=list)
    items: list[ChiefContextEvidenceItem] = Field(default_factory=list)
    complete: bool = False
    blockers: list[str] = Field(default_factory=list)
    live: bool = False

    @field_validator("required_sources", mode="before")
    @classmethod
    def _clean_required_sources(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        supported = {"gmail", "airtable", "work_items"}
        return list(
            dict.fromkeys(
                str(item or "").strip().lower()
                for item in values
                if str(item or "").strip().lower() in supported
            )
        )

    @field_validator("blockers", mode="before")
    @classmethod
    def _clean_blockers(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                _compact_text(item)
                for item in values
                if _compact_text(item)
            )
        )

    @model_validator(mode="after")
    def _validate_complete_bundle(self) -> ChiefContextEvidenceBundle:
        if not self.complete:
            return self
        receipts_by_source = {receipt.source: receipt for receipt in self.receipts}
        if not self.required_sources:
            raise ValueError("A complete Chief context bundle requires at least one source.")
        if set(receipts_by_source) != set(self.required_sources):
            raise ValueError(
                "A complete Chief context bundle requires one receipt per required source."
            )
        if any(
            not receipt.verified or receipt.status not in {"success", "empty"}
            for receipt in receipts_by_source.values()
        ):
            raise ValueError(
                "A complete Chief context bundle requires verified success or empty receipts."
            )
        if self.blockers:
            raise ValueError("A complete Chief context bundle cannot contain blockers.")
        return self
