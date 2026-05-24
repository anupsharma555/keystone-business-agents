"""Schemas for structuring retrieved web data before model synthesis."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

WebDataSourceType = Literal["structured_json", "text", "mixed", "unknown"]
WebFieldValueType = Literal["string", "number", "boolean", "date", "url", "currency", "unknown"]


class WebDataFieldSpec(BaseModel):
    """One requested field in a web-data structuring target schema."""

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    required: bool = False
    value_type: WebFieldValueType = "unknown"
    description: str = ""

    @field_validator("name", "description")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("aliases")
    @classmethod
    def _strip_aliases(cls, values: list[str]) -> list[str]:
        return [str(value or "").strip() for value in values if str(value or "").strip()]


class StructuredWebRecord(BaseModel):
    """One normalized web-derived record with provenance and issues."""

    source_id: str = ""
    source_url: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    raw_excerpt: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list)


class WebDataSchemaMappingResult(BaseModel):
    """Bounded helper output for model-safe web-data interpretation."""

    status: Literal["success", "partial", "no_data", "invalid_schema"] = "success"
    source_type: WebDataSourceType = "unknown"
    target_schema_name: str = ""
    fields: list[WebDataFieldSpec] = Field(default_factory=list)
    records: list[StructuredWebRecord] = Field(default_factory=list)
    unmapped_fields: list[str] = Field(default_factory=list)
    data_quality_issues: list[str] = Field(default_factory=list)
    send_enabled: bool = False
