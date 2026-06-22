"""Structured web-query planning schema."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class WebQueryPlan(BaseModel):
    """Bounded query-expansion plan for source-backed web retrieval."""

    source: str = "heuristic"
    subject: str = ""
    request_text: str = ""
    queries: list[str] = Field(default_factory=list, min_length=1, max_length=12)
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator("source", "subject", "request_text", "rationale", mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator("queries", "planner_warnings", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = " ".join(str(item or "").replace("\u2014", "-").split()).strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text[:240])
        return cleaned
