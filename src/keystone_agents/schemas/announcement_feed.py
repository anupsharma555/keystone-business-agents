"""Schemas for durable RSS/preprint announcement review records."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


def announcement_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AnnouncementFeedEvidence(BaseModel):
    """Bounded public evidence attached to a feed/preprint review item."""

    kind: str = "search"
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    status: str = "success"
    char_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnnouncementFeedItem(BaseModel):
    """Canonical durable record for one RSS, preprint, or announcement item."""

    canonical_key: str = ""
    publication_id: str = ""
    publication_id_type: str = ""
    doi: str = ""
    arxiv_id: str = ""
    biorxiv_id: str = ""
    medrxiv_id: str = ""
    title: str
    url: str = ""
    canonical_url: str = ""
    source: str = ""
    feed: str = ""
    authors: list[str] = Field(default_factory=list)
    published_at: str = ""
    tags: list[str] = Field(default_factory=list)
    relevance_status: str = "candidate"
    selected: bool = False
    selection_reason: str = ""
    summary: str = ""
    evidence: list[AnnouncementFeedEvidence] = Field(default_factory=list)
    extraction_status: str = "not_attempted"
    content_hash: str = ""
    automation_run_id: str = ""
    slack_link: str = ""
    review_metadata: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: str = Field(default_factory=announcement_timestamp)
    last_seen_at: str = Field(default_factory=announcement_timestamp)
    seen_count: int = 1

    @field_validator("title")
    @classmethod
    def _title_required(cls, value: str) -> str:
        cleaned = " ".join(str(value or "").strip().split())
        if not cleaned:
            raise ValueError("announcement feed item title is required")
        return cleaned

    def semantic_index_text(self) -> str:
        """Return public, source-visible text suitable for local retrieval indexes."""

        parts = [
            self.title,
            self.summary,
            self.selection_reason,
            self.source,
            self.feed,
            " ".join(self.tags),
            " ".join(evidence.snippet for evidence in self.evidence),
        ]
        return "\n".join(part for part in parts if part).strip()
