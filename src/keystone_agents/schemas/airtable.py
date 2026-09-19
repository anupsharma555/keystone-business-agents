"""Bounded Airtable schema summaries for prompt-safe agent context."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

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

KNOWN_AIRTABLE_FIELD_TYPES = frozenset(
    {
        "aiText",
        "autoNumber",
        "barcode",
        "button",
        "checkbox",
        "count",
        "createdBy",
        "createdTime",
        "currency",
        "date",
        "dateTime",
        "duration",
        "email",
        "externalSyncSource",
        "formula",
        "lastModifiedBy",
        "lastModifiedTime",
        "multilineText",
        "multipleAttachments",
        "multipleCollaborators",
        "multipleLookupValues",
        "multipleRecordLinks",
        "multipleSelects",
        "number",
        "percent",
        "phoneNumber",
        "rating",
        "richText",
        "rollup",
        "singleCollaborator",
        "singleLineText",
        "singleSelect",
        "url",
    }
)

AIRTABLE_SCHEMA_MAX_TABLES = 12
AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE = 20
AIRTABLE_SCHEMA_MAX_CHOICES = 20
AIRTABLE_SCHEMA_MAX_REFERENCED_FIELDS = 20
AIRTABLE_SCHEMA_PREVIEW_CHARS = 240


def _clean_text(value: object, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _exact_text(value: object) -> str:
    return str(value or "")


def _exact_text_preview(value: object, *, max_chars: int) -> tuple[str, dict[str, Any]]:
    text = _exact_text(value)
    end = min(len(text), max_chars)
    return text[:end], {
        "unit": "unicode_characters",
        "start": 0,
        "end": end,
        "full_count": len(text),
        "has_more": end < len(text),
        "complete": end == len(text),
        "next_request": None,
    }


def _source_value_state(options: Mapping[str, Any], key: str) -> str:
    if key not in options:
        return "omitted"
    value = options.get(key)
    if value is None:
        return "null"
    if value is False:
        return "false"
    if value == 0 and not isinstance(value, bool):
        return "zero"
    if value == "" or value == [] or value == {}:
        return "empty"
    return "value"


def airtable_schema_snapshot_sha256(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _bounded_mapping(value: object, *, max_chars: int = 1200) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload = dict(value)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if len(serialized) <= max_chars:
        return payload
    return {
        "preview_json": serialized[:max_chars],
        "truncated": True,
        "full_char_count": len(serialized),
    }


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
    field_mode: Literal["computed", "manual", "unknown"] = "unknown"
    is_computed: bool = False
    is_manual: bool = True
    is_valid: bool | None = None
    validity: Literal["valid", "invalid", "unknown"] = "unknown"
    linked_table_ids: list[str] = Field(default_factory=list)
    linked_table_id: str = ""
    inverse_link_field_id: str = ""
    record_link_field_id: str = ""
    field_id_in_linked_table: str = ""
    referenced_field_ids: list[str] = Field(default_factory=list)
    select_choices: list[str] = Field(default_factory=list)
    select_choices_exact: list[str] = Field(default_factory=list)
    formula: str = ""
    result_type: str = ""
    result_options: dict[str, Any] = Field(default_factory=dict)
    option_value_states: dict[str, str] = Field(default_factory=dict)
    description_coverage: dict[str, Any] = Field(default_factory=dict)
    formula_coverage: dict[str, Any] = Field(default_factory=dict)
    select_choices_coverage: dict[str, Any] = Field(default_factory=dict)
    referenced_field_ids_coverage: dict[str, Any] = Field(default_factory=dict)
    detail_read_required: bool = False
    detail_read_request: dict[str, Any] | None = None

    @field_validator(
        "field_id",
        "name",
        "field_type",
        "result_type",
        "linked_table_id",
        "inverse_link_field_id",
        "record_link_field_id",
        "field_id_in_linked_table",
        mode="before",
    )
    @classmethod
    def _preserve_exact_fields(cls, value: object) -> str:
        return _exact_text(value)

    @field_validator("description", "formula", mode="before")
    @classmethod
    def _preserve_bounded_exact_text(cls, value: object) -> str:
        return _exact_text(value)

    @field_validator(
        "linked_table_ids",
        "referenced_field_ids",
        "select_choices",
        "select_choices_exact",
        mode="before",
    )
    @classmethod
    def _preserve_exact_lists(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return [_exact_text(item) for item in values if _exact_text(item)]

    @model_validator(mode="after")
    def _derive_manual_flag(self) -> AirtableFieldSummary:
        if self.field_mode == "computed":
            self.is_computed = True
            self.is_manual = False
        elif self.field_mode == "manual":
            self.is_computed = False
            self.is_manual = True
        elif self.is_computed:
            self.field_mode = "computed"
            self.is_manual = False
        elif self.is_manual:
            self.field_mode = "manual"
        return self


class AirtableTableSummary(BaseModel):
    """Prompt-safe summary of one Airtable table."""

    model_config = ConfigDict(extra="forbid")

    table_id: str = ""
    name: str = Field(min_length=1)
    description: str = ""
    primary_field_id: str = ""
    fields: list[AirtableFieldSummary] = Field(default_factory=list)
    total_field_count: int = 0
    fields_coverage: dict[str, Any] = Field(default_factory=dict)
    description_coverage: dict[str, Any] = Field(default_factory=dict)
    detail_read_request: dict[str, Any] | None = None

    @field_validator("table_id", "name", "description", "primary_field_id", mode="before")
    @classmethod
    def _preserve_exact_fields(cls, value: object) -> str:
        return _exact_text(value)

    @property
    def field_count(self) -> int:
        return self.total_field_count or len(self.fields)


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
    source_snapshot_sha256: str = ""
    total_table_count: int = 0
    tables_coverage: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_id", "base_name", "source", mode="before")
    @classmethod
    def _preserve_exact_fields(cls, value: object) -> str:
        return _exact_text(value)

    @field_validator("allowed_tables", "missing_allowed_tables", "extra_tables", mode="before")
    @classmethod
    def _preserve_exact_lists(cls, value: object) -> list[str]:
        values = value if isinstance(value, list | tuple | set) else [value]
        return [_exact_text(item) for item in values if _exact_text(item)][:50]

    @property
    def table_count(self) -> int:
        return self.total_table_count or len(self.tables)


def airtable_field_summary_from_metadata(
    field: Mapping[str, Any],
    *,
    base_id: str = "",
    table_id: str = "",
    source_snapshot_sha256: str = "",
) -> AirtableFieldSummary:
    """Build a bounded field summary from Airtable metadata API field JSON."""

    options = field.get("options") if isinstance(field.get("options"), Mapping) else {}
    field_type = _exact_text(field.get("type"))
    raw_description = _exact_text(field.get("description"))
    description, description_coverage = _exact_text_preview(
        raw_description,
        max_chars=AIRTABLE_SCHEMA_PREVIEW_CHARS,
    )
    raw_formula = _exact_text(options.get("formula"))
    formula, formula_coverage = _exact_text_preview(
        raw_formula,
        max_chars=AIRTABLE_SCHEMA_PREVIEW_CHARS,
    )
    exact_choices: list[str] = []
    raw_choices = options.get("choices") if isinstance(options, Mapping) else None
    if isinstance(raw_choices, Sequence) and not isinstance(raw_choices, str):
        exact_choices = [
            str(choice.get("name") if isinstance(choice, Mapping) else choice)
            for choice in raw_choices
        ]
    visible_choices = exact_choices[:AIRTABLE_SCHEMA_MAX_CHOICES]
    detail_request = {
        "base_id": base_id,
        "table_id": table_id,
        "field_id": _exact_text(field.get("id")),
        "read_mode": "field_detail",
        "start_char": 0,
        "max_chars": 2000,
        "expected_source_sha256": source_snapshot_sha256,
    }
    select_choices_coverage = {
        "unit": "select_choice",
        "start": 0,
        "end": len(visible_choices),
        "full_count": len(exact_choices),
        "has_more": len(visible_choices) < len(exact_choices),
        "complete": len(visible_choices) == len(exact_choices),
        "next_request": detail_request if len(visible_choices) < len(exact_choices) else None,
    }
    raw_referenced_ids = options.get("referencedFieldIds")
    referenced_ids = (
        [_exact_text(item) for item in raw_referenced_ids]
        if isinstance(raw_referenced_ids, Sequence) and not isinstance(raw_referenced_ids, str)
        else []
    )
    visible_referenced_ids = referenced_ids[:AIRTABLE_SCHEMA_MAX_REFERENCED_FIELDS]
    referenced_field_ids_coverage = {
        "unit": "field_id",
        "start": 0,
        "end": len(visible_referenced_ids),
        "full_count": len(referenced_ids),
        "has_more": len(visible_referenced_ids) < len(referenced_ids),
        "complete": len(visible_referenced_ids) == len(referenced_ids),
        "next_request": (
            detail_request if len(visible_referenced_ids) < len(referenced_ids) else None
        ),
    }
    for coverage in (description_coverage, formula_coverage):
        if coverage["has_more"]:
            coverage["next_request"] = detail_request
    linked_table_id = _exact_text(options.get("linkedTableId"))
    is_valid = options.get("isValid") if "isValid" in options else None
    result = options.get("result") if isinstance(options.get("result"), Mapping) else None
    if field_type in COMPUTED_AIRTABLE_FIELD_TYPES:
        field_mode: Literal["computed", "manual", "unknown"] = "computed"
    elif field_type in KNOWN_AIRTABLE_FIELD_TYPES:
        field_mode = "manual"
    else:
        field_mode = "unknown"
    option_keys = (
        "linkedTableId",
        "inverseLinkFieldId",
        "recordLinkFieldId",
        "fieldIdInLinkedTable",
        "isValid",
        "referencedFieldIds",
        "result",
        "choices",
        "formula",
    )
    return AirtableFieldSummary(
        field_id=str(field.get("id") or ""),
        name=str(field.get("name") or "Unnamed Field"),
        field_type=field_type,
        description=description,
        field_mode=field_mode,
        is_computed=field_type in COMPUTED_AIRTABLE_FIELD_TYPES,
        is_manual=field_mode == "manual",
        is_valid=is_valid if isinstance(is_valid, bool) else None,
        validity=("valid" if is_valid is True else "invalid" if is_valid is False else "unknown"),
        linked_table_ids=[linked_table_id] if linked_table_id else [],
        linked_table_id=linked_table_id,
        inverse_link_field_id=_exact_text(options.get("inverseLinkFieldId")),
        record_link_field_id=_exact_text(options.get("recordLinkFieldId")),
        field_id_in_linked_table=_exact_text(options.get("fieldIdInLinkedTable")),
        referenced_field_ids=visible_referenced_ids,
        select_choices=visible_choices,
        select_choices_exact=visible_choices,
        formula=formula,
        result_type=_exact_text(result.get("type")) if result else "",
        result_options=_bounded_mapping(result.get("options")) if result else {},
        option_value_states={key: _source_value_state(options, key) for key in option_keys},
        description_coverage=description_coverage,
        formula_coverage=formula_coverage,
        select_choices_coverage=select_choices_coverage,
        referenced_field_ids_coverage=referenced_field_ids_coverage,
        detail_read_required=any(
            coverage.get("has_more")
            for coverage in (
                description_coverage,
                formula_coverage,
                select_choices_coverage,
                referenced_field_ids_coverage,
            )
        ),
        detail_read_request=detail_request,
    )


def airtable_table_summary_from_metadata(
    table: Mapping[str, Any],
    *,
    base_id: str = "",
    source_snapshot_sha256: str = "",
) -> AirtableTableSummary:
    """Build a bounded table summary from Airtable metadata API table JSON."""

    fields = table.get("fields") if isinstance(table.get("fields"), Sequence) else []
    visible_fields = [field for field in fields if isinstance(field, Mapping)][
        :AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE
    ]
    table_id = _exact_text(table.get("id"))
    total_field_count = len([field for field in fields if isinstance(field, Mapping)])
    description, description_coverage = _exact_text_preview(
        table.get("description"),
        max_chars=AIRTABLE_SCHEMA_PREVIEW_CHARS,
    )
    table_detail_request = {
        "base_id": base_id,
        "table_id": table_id,
        "read_mode": "table_detail",
        "start_char": 0,
        "max_chars": 2000,
        "expected_source_sha256": source_snapshot_sha256,
    }
    if description_coverage["has_more"]:
        description_coverage["next_request"] = table_detail_request
    next_request = {
        "base_id": base_id,
        "table_id": table_id,
        "read_mode": "field_index",
        "field_start": len(visible_fields),
        "max_fields": AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE,
        "expected_source_sha256": source_snapshot_sha256,
    }
    return AirtableTableSummary(
        table_id=table_id,
        name=str(table.get("name") or "Unnamed Table"),
        description=description,
        primary_field_id=str(table.get("primaryFieldId") or ""),
        fields=[
            airtable_field_summary_from_metadata(
                field,
                base_id=base_id,
                table_id=table_id,
                source_snapshot_sha256=source_snapshot_sha256,
            )
            for field in visible_fields
        ],
        total_field_count=total_field_count,
        fields_coverage={
            "unit": "field",
            "start": 0,
            "end": len(visible_fields),
            "full_count": total_field_count,
            "has_more": len(visible_fields) < total_field_count,
            "complete": len(visible_fields) == total_field_count,
            "next_request": next_request if len(visible_fields) < total_field_count else None,
        },
        description_coverage=description_coverage,
        detail_read_request=table_detail_request,
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
    source_snapshot_sha256 = airtable_schema_snapshot_sha256(payload)
    all_tables = [table for table in raw_tables if isinstance(table, Mapping)]
    visible_tables = all_tables[:AIRTABLE_SCHEMA_MAX_TABLES]
    tables = [
        airtable_table_summary_from_metadata(
            table,
            base_id=base_id,
            source_snapshot_sha256=source_snapshot_sha256,
        )
        for table in visible_tables
    ]
    all_table_names = [
        _exact_text(table.get("name")) for table in all_tables if _exact_text(table.get("name"))
    ]
    table_names = set(all_table_names)
    allowed = [_exact_text(table) for table in allowed_tables if _exact_text(table)]
    table_next_request = {
        "base_id": base_id,
        "read_mode": "table_index",
        "table_start": len(visible_tables),
        "max_tables": AIRTABLE_SCHEMA_MAX_TABLES,
        "expected_source_sha256": source_snapshot_sha256,
    }
    return AirtableBaseSchemaSummary(
        base_id=base_id,
        base_name=base_name,
        allowed_tables=allowed,
        tables=tables,
        missing_allowed_tables=[table for table in allowed if table not in table_names],
        extra_tables=[name for name in all_table_names if name not in set(allowed)],
        source_snapshot_sha256=source_snapshot_sha256,
        total_table_count=len(all_tables),
        tables_coverage={
            "unit": "table",
            "start": 0,
            "end": len(visible_tables),
            "full_count": len(all_tables),
            "has_more": len(visible_tables) < len(all_tables),
            "complete": len(visible_tables) == len(all_tables),
            "next_request": (table_next_request if len(visible_tables) < len(all_tables) else None),
        },
    )


def airtable_field_preview_payload(field: AirtableFieldSummary) -> dict[str, Any]:
    """Serialize one compact field preview without hiding meaningful source state."""

    payload: dict[str, Any] = {
        "field_id": field.field_id,
        "name": field.name,
        "field_type": field.field_type,
        "field_mode": field.field_mode,
        "is_computed": field.is_computed,
        "is_manual": field.is_manual,
    }
    meaningful_states = {
        key: state for key, state in field.option_value_states.items() if state != "omitted"
    }
    if field.field_mode == "computed" or "isValid" in meaningful_states:
        payload["validity"] = field.validity
        payload["is_valid"] = field.is_valid
    for key in (
        "description",
        "linked_table_id",
        "inverse_link_field_id",
        "record_link_field_id",
        "field_id_in_linked_table",
        "formula",
        "result_type",
    ):
        value = getattr(field, key)
        if value:
            payload[key] = value
    if field.linked_table_ids:
        payload["linked_table_ids"] = field.linked_table_ids
    if field.referenced_field_ids:
        payload["referenced_field_ids"] = field.referenced_field_ids
    if field.select_choices_exact:
        payload["select_choices_exact"] = field.select_choices_exact
    if field.result_options:
        payload["result_options"] = field.result_options
    if meaningful_states:
        payload["option_value_states"] = meaningful_states
    coverage: dict[str, Any] = {}
    for name, value in (
        ("description", field.description_coverage),
        ("formula", field.formula_coverage),
        ("select_choices", field.select_choices_coverage),
        ("referenced_field_ids", field.referenced_field_ids_coverage),
    ):
        if value.get("has_more"):
            coverage[name] = value
    if coverage:
        payload["coverage"] = coverage
    if field.detail_read_required:
        payload["detail_read_required"] = True
        payload["detail_read_request"] = field.detail_read_request
    return payload


def airtable_schema_preview_payload(summary: AirtableBaseSchemaSummary) -> dict[str, Any]:
    """Serialize bounded schema discovery without repeating empty field defaults."""

    payload: dict[str, Any] = {
        "base_id": summary.base_id,
        "base_name": summary.base_name,
        "allowed_tables": summary.allowed_tables,
        "tables": [],
        "source": summary.source,
        "source_snapshot_sha256": summary.source_snapshot_sha256,
        "total_table_count": summary.table_count,
    }
    if summary.missing_allowed_tables:
        payload["missing_allowed_tables"] = summary.missing_allowed_tables
    if summary.extra_tables:
        payload["extra_tables"] = summary.extra_tables
    if summary.tables_coverage.get("has_more"):
        payload["tables_coverage"] = summary.tables_coverage
    for table in summary.tables:
        table_payload: dict[str, Any] = {
            "table_id": table.table_id,
            "name": table.name,
            "primary_field_id": table.primary_field_id,
            "total_field_count": table.field_count,
            "fields": [airtable_field_preview_payload(field) for field in table.fields],
        }
        if table.description:
            table_payload["description"] = table.description
        if table.description_coverage.get("has_more"):
            table_payload["description_coverage"] = table.description_coverage
            table_payload["detail_read_request"] = table.detail_read_request
        if table.fields_coverage.get("has_more"):
            table_payload["fields_coverage"] = table.fields_coverage
        payload["tables"].append(table_payload)
    return payload


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
                f"- Provider field count: {table.field_count}",
                (
                    "- Field preview coverage: "
                    f"{table.fields_coverage.get('end', len(table.fields))}/"
                    f"{table.fields_coverage.get('full_count', table.field_count)}"
                ),
            ]
        )
        for field in table.fields:
            mode = field.field_mode
            lines.append(
                f"- `{field.name}` id=`{field.field_id}` "
                f"({field.field_type or 'unknown'}, {mode}, validity={field.validity})"
            )
            if field.linked_table_id:
                lines.append(f"  - linked table id: `{field.linked_table_id}`")
            if field.inverse_link_field_id:
                lines.append(f"  - inverse linked-table field id: `{field.inverse_link_field_id}`")
            if field.record_link_field_id:
                lines.append(
                    f"  - current-table record-link field id: `{field.record_link_field_id}`"
                )
            if field.field_id_in_linked_table:
                lines.append(
                    f"  - target field id in linked table: `{field.field_id_in_linked_table}`"
                )
            if field.detail_read_required:
                lines.append(
                    "  - exact detail read required for omitted description/formula/"
                    "choice/reference content"
                )
        if table.fields_coverage.get("has_more"):
            lines.append(
                "- Additional fields exist outside this bounded preview; use the saved "
                "continuation."
            )
    if summary.missing_allowed_tables:
        lines.extend(["", "## Missing Expected Tables", ""])
        lines.extend(f"- {table}" for table in summary.missing_allowed_tables)
    return "\n".join(lines).strip() + "\n"
