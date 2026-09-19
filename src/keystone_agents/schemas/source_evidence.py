"""Bounded pages of retained source evidence, separate from model previews."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SourceEvidencePage(BaseModel):
    """One sanitized source fragment and its original coverage/provenance metadata."""

    model_config = ConfigDict(extra="forbid")

    evidence_excerpt: str = Field(max_length=3_000)
    provenance_json: str = Field(default="{}", max_length=32_000)


class SourceEvidenceAccess(BaseModel):
    """Compact snapshot identity for the request-bound local evidence reader."""

    model_config = ConfigDict(extra="forbid")

    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    retained_char_count: int = Field(ge=0)
    read_tool: Literal["read_work_item_source_evidence"] = "read_work_item_source_evidence"


def source_evidence_access(pages: list[SourceEvidencePage]) -> SourceEvidenceAccess | None:
    if not pages:
        return None
    serialized = json.dumps(
        [page.model_dump(mode="json") for page in pages],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return SourceEvidenceAccess(
        snapshot_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
        page_count=len(pages),
        retained_char_count=sum(len(page.evidence_excerpt) for page in pages),
    )


def compact_source_evidence_context(value: Any) -> Any:
    """Remove retained pages from prompt projections, keeping their exact access handle."""
    if isinstance(value, dict):
        return {
            key: [] if key == "evidence_pages" else compact_source_evidence_context(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [compact_source_evidence_context(item) for item in value]
    return value
