"""Structured Opportunity Scout search-planning schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

OpportunityTargetEntity = Literal[
    "company",
    "institute",
    "researcher",
    "conference",
    "grant_program",
    "trial",
    "role",
]

OpportunitySearchObjective = Literal[
    "company_growth",
    "research_collaboration",
    "institute_partnership",
    "presentation_opportunity",
    "advisory",
    "funding",
    "hiring",
    "trial_collaboration",
    "broad_discovery",
]


class OpportunitySearchPlan(BaseModel):
    """Pre-retrieval semantic plan for Opportunity Scout live search."""

    source: str = "heuristic"
    desired_count: int = Field(default=5, ge=1, le=10)
    target_entity_types: list[OpportunityTargetEntity] = Field(default_factory=list)
    objectives: list[OpportunitySearchObjective] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    must_include_terms: list[str] = Field(default_factory=list)
    exclude_entity_types: list[OpportunityTargetEntity] = Field(default_factory=list)
    strict_targeting: bool = False
    rationale: str = ""
    planner_warnings: list[str] = Field(default_factory=list)

    @field_validator(
        "target_entity_types",
        "objectives",
        "domains",
        "must_include_terms",
        "exclude_entity_types",
        "planner_warnings",
        mode="before",
    )
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        values = value if isinstance(value, list | tuple | set) else [value]
        return [
            str(item).replace("\u2014", "-").strip() for item in values if str(item or "").strip()
        ]

    @field_validator("source", mode="before")
    @classmethod
    def _clean_source(cls, value: object) -> str:
        return str(value or "heuristic").replace("\u2014", "-").strip() or "heuristic"

    @field_validator("desired_count", mode="before")
    @classmethod
    def _clean_desired_count(cls, value: object) -> int:
        try:
            return max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return 5
