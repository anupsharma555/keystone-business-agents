"""Retrieval-hint schema shared across orchestrator and specialists."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RetrievalHint(BaseModel):
    """Control-plane retrieval recommendation for hybrid search escalation."""

    source: str = "control_plane"
    needs_precision_search: bool = False
    needs_structured_enrichment: bool = False
    needs_search_review: bool = False
    reasons: list[str] = Field(default_factory=list)

    @field_validator("source", mode="before")
    @classmethod
    def _clean_source(cls, value: object) -> str:
        return str(value or "control_plane").replace("\u2014", "-").strip() or "control_plane"

    @field_validator("reasons", mode="before")
    @classmethod
    def _clean_reasons(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]
