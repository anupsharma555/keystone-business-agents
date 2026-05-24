"""Bounded Airtable schema summaries for prompt-safe agent context."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FINANCE_TAX_TRACKER_BASE_NAME = "2026 Finance & Tax Tracker"
FINANCE_TAX_TRACKER_TABLES: tuple[str, ...] = (
    "Business Income",
    "Business Expenses",
    "Personal Income",
    "Personal Expenses",
    "Tax Payments",
)

COMPUTED_AIRTABLE_FIELD_TYPES = frozenset(
    {
        "aiText",
        "autoNumber",
        "button",
        "count",
        "createdBy",
        "createdTime",
        "externalSyncSource",
        "formula",
        "lastModifiedBy",
        "lastModifiedTime",
        "lookup",
        "multipleLookupValues",
        "rollup",
    }
)


def _clean_text(value: object, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _clean_list(values: object, *, max_items: int = 20, max_chars: int = 120) -> list[str]:
    if values is None:
        return []
    items = values if isinstance(values, list | tuple | set) else [values]
    cleaned = [_clean_text(item, max_chars=max_chars) for item in items]
    return [item for item in cleaned if item][:max_items]


class AirtableFieldSummary(BaseModel):
    """Prompt-safe summary of one Airtable field."""

    model_config = ConfigDict(extra="forbid")

    field_id: str = ""
    name: str = Field(min_length=1)
    field_type: str = ""
    description: str = ""
    is_computed: bool = False
    is_manual: bool = True
    linked_table_ids: list[str] = Field(default_factory=list)
    select_choices: list[str] = Field(default_factory=list)
    formula: str = ""
    result_type: str = ""

    @field_validator(
        "field_id",
        "name",
        "field_type",
        "description",
        "formula",
        "result_type",
        mode="before",
    )
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("linked_table_ids", "select_choices", mode="before")
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value)

    @model_validator(mode="after")
    def _derive_manual_flag(self) -> AirtableFieldSummary:
        self.is_manual = not self.is_computed
        return self


class AirtableTableSummary(BaseModel):
    """Prompt-safe summary of one Airtable table."""

    model_config = ConfigDict(extra="forbid")

    table_id: str = ""
    name: str = Field(min_length=1)
    description: str = ""
    primary_field_id: str = ""
    fields: list[AirtableFieldSummary] = Field(default_factory=list)

    @field_validator("table_id", "name", "description", "primary_field_id", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @property
    def field_count(self) -> int:
        return len(self.fields)


class AirtableBaseSchemaSummary(BaseModel):
    """Prompt-safe summary of a configured Airtable base."""

    model_config = ConfigDict(extra="forbid")

    base_id: str = ""
    base_name: str = FINANCE_TAX_TRACKER_BASE_NAME
    allowed_tables: list[str] = Field(default_factory=lambda: list(FINANCE_TAX_TRACKER_TABLES))
    tables: list[AirtableTableSummary] = Field(default_factory=list)
    missing_allowed_tables: list[str] = Field(default_factory=list)
    extra_tables: list[str] = Field(default_factory=list)
    source: str = "airtable_schema"

    @field_validator("base_id", "base_name", "source", mode="before")
    @classmethod
    def _clean_fields(cls, value: object) -> str:
        return _clean_text(value)

    @field_validator("allowed_tables", "missing_allowed_tables", "extra_tables", mode="before")
    @classmethod
    def _clean_lists(cls, value: object) -> list[str]:
        return _clean_list(value, max_items=50)

    @property
    def table_count(self) -> int:
        return len(self.tables)


def airtable_field_summary_from_metadata(field: Mapping[str, Any]) -> AirtableFieldSummary:
    """Build a bounded field summary from Airtable metadata API field JSON."""

    options = field.get("options") if isinstance(field.get("options"), Mapping) else {}
    field_type = _clean_text(field.get("type"), max_chars=80)
    choices = []
    raw_choices = options.get("choices") if isinstance(options, Mapping) else None
    if isinstance(raw_choices, Sequence) and not isinstance(raw_choices, str):
        choices = [
            _clean_text(choice.get("name") if isinstance(choice, Mapping) else choice)
            for choice in raw_choices
        ]
    linked_table_ids = []
    if isinstance(options, Mapping):
        linked_table_ids = _clean_list(
            [
                options.get("linkedTableId"),
                options.get("inverseLinkFieldId"),
            ],
            max_items=5,
        )
    return AirtableFieldSummary(
        field_id=str(field.get("id") or ""),
        name=str(field.get("name") or "Unnamed Field"),
        field_type=field_type,
        description=str(field.get("description") or ""),
        is_computed=field_type in COMPUTED_AIRTABLE_FIELD_TYPES,
        linked_table_ids=linked_table_ids,
        select_choices=choices,
        formula=str(options.get("formula") or "") if isinstance(options, Mapping) else "",
        result_type=str(options.get("result", {}).get("type") or "")
        if isinstance(options.get("result"), Mapping)
        else "",
    )


def airtable_table_summary_from_metadata(table: Mapping[str, Any]) -> AirtableTableSummary:
    """Build a bounded table summary from Airtable metadata API table JSON."""

    fields = table.get("fields") if isinstance(table.get("fields"), Sequence) else []
    return AirtableTableSummary(
        table_id=str(table.get("id") or ""),
        name=str(table.get("name") or "Unnamed Table"),
        description=str(table.get("description") or ""),
        primary_field_id=str(table.get("primaryFieldId") or ""),
        fields=[
            airtable_field_summary_from_metadata(field)
            for field in fields
            if isinstance(field, Mapping)
        ],
    )


def airtable_base_schema_summary_from_metadata(
    payload: Mapping[str, Any],
    *,
    base_id: str = "",
    base_name: str = FINANCE_TAX_TRACKER_BASE_NAME,
    allowed_tables: Sequence[str] = FINANCE_TAX_TRACKER_TABLES,
) -> AirtableBaseSchemaSummary:
    """Build a bounded base summary from Airtable metadata API payload JSON."""

    raw_tables = payload.get("tables") if isinstance(payload.get("tables"), Sequence) else []
    tables = [
        airtable_table_summary_from_metadata(table)
        for table in raw_tables
        if isinstance(table, Mapping)
    ]
    table_names = {table.name for table in tables}
    allowed = [_clean_text(table, max_chars=120) for table in allowed_tables if _clean_text(table)]
    return AirtableBaseSchemaSummary(
        base_id=base_id,
        base_name=base_name,
        allowed_tables=allowed,
        tables=tables,
        missing_allowed_tables=[table for table in allowed if table not in table_names],
        extra_tables=[table.name for table in tables if table.name not in set(allowed)],
    )


def airtable_schema_context_markdown(summary: AirtableBaseSchemaSummary) -> str:
    """Render a compact internal context document from a bounded schema summary."""

    lines = [
        f"# {summary.base_name}",
        "",
        "Internal Chief of Staff context for the finance and tax tracker.",
        "",
        "## Guardrails",
        "",
        "- Use this Airtable base as an internal operating surface, not canonical tax advice.",
        "- Read schema before writing; do not write when table, field, or record identity is "
        "uncertain.",
        "- Do not delete records, change schema, file returns, make payments, or claim final "
        "tax treatment.",
        "- Flag uncertain deductions, mixed-use expenses, entity-structure issues, "
        "estimated-tax questions, and Philadelphia BIRT/NPT matters for human tax review.",
        "",
        "## Tables",
    ]
    for table in summary.tables:
        allowed_note = "allowed" if table.name in summary.allowed_tables else "not in allowed list"
        lines.extend(
            [
                "",
                f"### {table.name}",
                "",
                f"- Airtable table id: `{table.table_id}`",
                f"- Status: {allowed_note}",
                f"- Field count: {table.field_count}",
            ]
        )
        for field in table.fields:
            mode = "computed" if field.is_computed else "manual"
            lines.append(f"- `{field.name}` ({field.field_type or 'unknown'}, {mode})")
    if summary.missing_allowed_tables:
        lines.extend(["", "## Missing Expected Tables", ""])
        lines.extend(f"- {table}" for table in summary.missing_allowed_tables)
    return "\n".join(lines).strip() + "\n"
