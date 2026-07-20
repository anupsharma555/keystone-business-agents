"""General research brief schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from keystone_agents.schemas.request_coverage import RequestCoverage

ResearchTargetType = Literal[
    "company",
    "institute",
    "conference",
    "lab",
    "person",
    "zotero_collection",
    "zotero_article",
    "article_collection",
    "github_repository_collection",
    "topic",
    "other",
]


class ResearchBriefFact(BaseModel):
    """One source-cited factual claim in a general research brief."""

    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_ids")
    @classmethod
    def clean_source_ids(cls, source_ids: list[str]) -> list[str]:
        return list(
            dict.fromkeys(source_id.strip() for source_id in source_ids if source_id.strip())
        )


class ResearchSourceCitation(BaseModel):
    """Compact source citation for public, local, or fixture research context."""

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: str = ""

    @field_validator("source_id", "title", "url", "source_type")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class ResearchArticleSummary(BaseModel):
    """Per-article summary for Zotero or literature collection review."""

    title: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    research_question: str = ""
    methods_or_design: str = ""
    key_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    relevance_to_goal: str = ""

    @field_validator(
        "title",
        "research_question",
        "methods_or_design",
        "relevance_to_goal",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator("source_ids", "key_findings", "limitations", mode="before")
    @classmethod
    def clean_text_list(cls, values: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip())
        )


class ResearchBrief(BaseModel):
    """Source-backed brief for companies, institutions, conferences, topics, or collections."""

    target_name: str = Field(min_length=1)
    target_type: ResearchTargetType = "other"
    research_goal: str = ""
    summary: str = ""
    key_findings: list[str] = Field(default_factory=list)
    article_summaries: list[ResearchArticleSummary] = Field(default_factory=list)
    facts: list[ResearchBriefFact] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    source_ids_used: list[str] = Field(default_factory=list)
    sources: list[ResearchSourceCitation] = Field(default_factory=list)
    raw_source_content_included: bool = False
    send_enabled: bool = False
    request_coverage: RequestCoverage = Field(default_factory=RequestCoverage)

    @field_validator("target_name", "research_goal", "summary", mode="before")
    @classmethod
    def clean_text(cls, value: str | None) -> str:
        return str(value or "").strip()

    @field_validator(
        "key_findings",
        "inferences",
        "unknowns",
        "limitations",
        "next_steps",
        "source_ids_used",
        mode="before",
    )
    @classmethod
    def clean_text_list(cls, values: list[str] | None) -> list[str]:
        return list(
            dict.fromkeys(str(value).strip() for value in values or [] if str(value).strip())
        )

    @field_validator("raw_source_content_included", "send_enabled")
    @classmethod
    def unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("research briefs must not include raw source text or sends")
        return value

    @model_validator(mode="after")
    def validate_source_citations(self) -> ResearchBrief:
        fact_source_ids = [source_id for fact in self.facts for source_id in fact.source_ids]
        article_source_ids = [
            source_id for article in self.article_summaries for source_id in article.source_ids
        ]
        self.source_ids_used = list(
            dict.fromkeys([*self.source_ids_used, *fact_source_ids, *article_source_ids])
        )
        cited_ids = {source.source_id for source in self.sources}
        uncited = sorted(
            source_id for source_id in self.source_ids_used if source_id not in cited_ids
        )
        if self.sources and uncited:
            self.source_ids_used = [
                source_id for source_id in self.source_ids_used if source_id in cited_ids
            ]
            self.unknowns = list(
                dict.fromkeys(
                    [
                        *self.unknowns,
                        (
                            "Some model-cited source ids were not returned in the "
                            "validated source list and require source review: "
                            f"{', '.join(uncited)}."
                        ),
                    ]
                )
            )
        return self
