"""Structured output for vector-store retrieval and grounded synthesis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RAGRetrievalMode = Literal["single_article", "semantic_search", "hybrid"]
RAGMatchStatus = Literal[
    "matched",
    "ambiguous",
    "not_found",
    "insufficient_evidence",
    "fixture_not_queried",
]


class RAGSourceMatch(BaseModel):
    """One ranked vector-store result retained by the specialist."""

    rank: int = Field(ge=1)
    source_id: str = Field(min_length=1)
    title: str = Field(default="", max_length=500)
    file_id: str = Field(default="", max_length=200)
    filename: str = Field(default="", max_length=500)
    semantic_relevance: str = Field(default="", max_length=1_000)
    evidence_excerpt: str = Field(default="", max_length=1_200)
    similarity_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator(
        "source_id",
        "title",
        "file_id",
        "filename",
        "semantic_relevance",
        "evidence_excerpt",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").split())


class RAGGroundedClaim(BaseModel):
    """One answer claim tied to retained vector-store matches."""

    text: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

    @field_validator("text", mode="before")
    @classmethod
    def clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @field_validator("source_ids", mode="before")
    @classmethod
    def clean_source_ids(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip())
        )


class RAGRetrievalResult(BaseModel):
    """Ranked vector-store retrieval result with source-grounded synthesis."""

    query: str = Field(min_length=1)
    retrieval_mode: RAGRetrievalMode = "semantic_search"
    match_status: RAGMatchStatus = "insufficient_evidence"
    answer: str = ""
    matches: list[RAGSourceMatch] = Field(default_factory=list, max_length=20)
    claims: list[RAGGroundedClaim] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggested_follow_up_queries: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    file_search_performed: bool = False
    external_write_performed: bool = False
    send_enabled: bool = False

    @field_validator(
        "query",
        "answer",
        mode="before",
    )
    @classmethod
    def clean_text(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @field_validator(
        "unknowns",
        "limitations",
        "suggested_follow_up_queries",
        mode="before",
    )
    @classmethod
    def clean_text_list(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                " ".join(str(item or "").split())
                for item in values
                if str(item or "").strip()
            )
        )

    @field_validator("external_write_performed", "send_enabled")
    @classmethod
    def unsafe_flags_must_be_false(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError(
                "RAG retrieval is read-only and cannot send or perform external writes"
            )
        return value

    @model_validator(mode="after")
    def validate_grounding(self) -> RAGRetrievalResult:
        source_ids = {match.source_id for match in self.matches}
        uncited = sorted(
            source_id
            for claim in self.claims
            for source_id in claim.source_ids
            if source_id not in source_ids
        )
        if uncited:
            raise ValueError(
                "RAG claims reference source ids absent from matches: " + ", ".join(uncited)
            )
        if self.match_status in {"matched", "ambiguous"} and not self.matches:
            raise ValueError("matched or ambiguous RAG results require retained matches")
        if self.match_status == "not_found" and (self.matches or self.claims):
            raise ValueError(
                "not_found RAG results cannot retain off-topic matches or grounded claims"
            )
        if self.file_search_performed and self.match_status == "fixture_not_queried":
            raise ValueError("fixture_not_queried cannot claim that file search ran")
        if self.match_status == "matched" and not self.answer:
            raise ValueError("matched RAG results require an answer")
        return self
