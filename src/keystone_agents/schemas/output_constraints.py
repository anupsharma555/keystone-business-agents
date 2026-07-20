"""Typed natural-language output constraints and validation results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ConstraintScope = Literal["unspecified", "answer", "entire_response", "draft_body"]
CountMode = Literal["unspecified", "exact", "maximum", "under", "minimum"]


class InterpretedOutputConstraints(BaseModel):
    """LLM-interpreted response requirements that survive agent handoffs."""

    interpretation: str = ""
    scope: ConstraintScope = "unspecified"
    word_count_mode: CountMode = "unspecified"
    word_count: int | None = Field(default=None, ge=1, le=5000)
    sentence_count_mode: CountMode = "unspecified"
    sentence_count: int | None = Field(default=None, ge=1, le=100)
    item_count_mode: CountMode = "unspecified"
    minimum_items: int | None = Field(default=None, ge=0, le=100)
    maximum_items: int | None = Field(default=None, ge=0, le=100)
    required_sections: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    forbid_em_dash: bool = False
    include_source_urls: bool = False
    style_requirements: list[str] = Field(default_factory=list)

    @field_validator("interpretation", mode="before")
    @classmethod
    def clean_interpretation(cls, value: object) -> str:
        return str(value or "").replace("\u2014", "-").strip()

    @field_validator(
        "required_sections",
        "forbidden_phrases",
        "style_requirements",
        mode="before",
    )
    @classmethod
    def clean_text_lists(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return list(
            dict.fromkeys(
                str(item).strip() for item in values if str(item or "").strip()
            )
        )

    @model_validator(mode="after")
    def validate_counts(self) -> InterpretedOutputConstraints:
        if self.word_count_mode != "unspecified" and self.word_count is None:
            raise ValueError("word_count is required when word_count_mode is explicit")
        if self.sentence_count_mode != "unspecified" and self.sentence_count is None:
            raise ValueError(
                "sentence_count is required when sentence_count_mode is explicit"
            )
        if (
            self.minimum_items is not None
            and self.maximum_items is not None
            and self.minimum_items > self.maximum_items
        ):
            raise ValueError("minimum_items cannot exceed maximum_items")
        return self

    def is_explicit(self) -> bool:
        return bool(
            self.interpretation
            or self.scope != "unspecified"
            or self.word_count_mode != "unspecified"
            or self.sentence_count_mode != "unspecified"
            or self.item_count_mode != "unspecified"
            or self.minimum_items is not None
            or self.maximum_items is not None
            or self.required_sections
            or self.forbidden_phrases
            or self.forbid_em_dash
            or self.include_source_urls
            or self.style_requirements
        )

    def has_deterministic_requirements(self) -> bool:
        """Return whether an objective validator has anything safe to enforce."""

        return bool(
            self.word_count_mode != "unspecified"
            or self.sentence_count_mode != "unspecified"
            or self.item_count_mode != "unspecified"
            or self.minimum_items is not None
            or self.maximum_items is not None
            or self.required_sections
            or self.forbidden_phrases
            or self.forbid_em_dash
            or self.include_source_urls
        )


class OutputConstraintValidation(BaseModel):
    """Objective validation of one LLM-produced response."""

    applicable: bool = False
    passed: bool = True
    scope: ConstraintScope = "unspecified"
    checked_text: str = ""
    word_count: int | None = None
    sentence_count: int | None = None
    item_count: int | None = None
    satisfied_constraints: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


__all__ = ["InterpretedOutputConstraints", "OutputConstraintValidation"]
