"""Helpers that normalize retrieved web data into bounded schemas."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.schemas.web_data import (
    StructuredWebRecord,
    WebDataFieldSpec,
    WebDataSchemaMappingResult,
)
from keystone_agents.sdk import function_tool

_LINE_VALUE_RE = re.compile(r"^\s*([^:\n]{2,80})\s*:\s*(.+?)\s*$")


def _json_payload(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _load_json(value: str, *, field_name: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON when provided") from exc


def _field_specs(target_schema: Any) -> tuple[str, list[WebDataFieldSpec]]:
    if target_schema is None:
        return "", []
    if isinstance(target_schema, list):
        fields = target_schema
        schema_name = ""
    elif isinstance(target_schema, Mapping):
        schema_name = str(target_schema.get("name") or target_schema.get("schema_name") or "")
        fields = target_schema.get("fields") or target_schema.get("columns") or []
    else:
        return "", []

    specs: list[WebDataFieldSpec] = []
    for item in fields:
        if isinstance(item, str):
            specs.append(WebDataFieldSpec(name=item))
        elif isinstance(item, Mapping):
            specs.append(WebDataFieldSpec.model_validate(dict(item)))
    return schema_name, specs


def _records_from_source(source_data: Any) -> tuple[str, list[Mapping[str, Any]], list[str]]:
    issues: list[str] = []
    if source_data is None:
        return "text", [], issues
    if isinstance(source_data, list):
        records = [item for item in source_data if isinstance(item, Mapping)]
        if len(records) != len(source_data):
            issues.append("Some JSON list items were not objects and were ignored.")
        return "structured_json", records, issues
    if isinstance(source_data, Mapping):
        for key in ("records", "rows", "items", "data"):
            value = source_data.get(key)
            if isinstance(value, list):
                records = [item for item in value if isinstance(item, Mapping)]
                return "structured_json", records, issues
        return "structured_json", [source_data], issues
    issues.append("Source JSON was not an object or list of objects.")
    return "unknown", [], issues


def _lookup_map(specs: list[WebDataFieldSpec]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for spec in specs:
        keys = [spec.name, *spec.aliases]
        for key in keys:
            normalized = _normalize_key(key)
            if normalized:
                lookup[normalized] = spec.name
    return lookup


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _bounded_excerpt(value: Any, *, max_chars: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def _map_record(
    record: Mapping[str, Any],
    *,
    specs: list[WebDataFieldSpec],
    source_id: str,
    source_url: str,
) -> tuple[StructuredWebRecord, list[str]]:
    lookup = _lookup_map(specs)
    fields: dict[str, Any] = {}
    unmapped: list[str] = []
    for key, value in record.items():
        target = lookup.get(_normalize_key(key))
        if target:
            fields[target] = value
        else:
            unmapped.append(str(key))

    issues = [
        f"Missing required field: {spec.name}"
        for spec in specs
        if spec.required and spec.name not in fields
    ]
    confidence = 0.9 if fields and not issues else 0.65 if fields else 0.2
    return (
        StructuredWebRecord(
            source_id=source_id,
            source_url=source_url,
            fields=fields,
            raw_excerpt=_bounded_excerpt(record),
            confidence=confidence,
            issues=issues,
        ),
        unmapped,
    )


def _record_from_text(
    text: str,
    *,
    specs: list[WebDataFieldSpec],
    source_id: str,
    source_url: str,
) -> StructuredWebRecord:
    lookup = _lookup_map(specs)
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE_VALUE_RE.match(line)
        if not match:
            continue
        target = lookup.get(_normalize_key(match.group(1)))
        if target and target not in fields:
            fields[target] = match.group(2).strip()

    issues = [
        f"Missing required field: {spec.name}"
        for spec in specs
        if spec.required and spec.name not in fields
    ]
    if not fields:
        issues.append("No explicit field:value lines matched the target schema.")
    return StructuredWebRecord(
        source_id=source_id,
        source_url=source_url,
        fields=fields,
        raw_excerpt=_bounded_excerpt(text),
        confidence=0.55 if fields and not issues else 0.35 if fields else 0.1,
        issues=issues,
    )


def structure_web_data_for_schema_impl(
    *,
    target_schema_json: str,
    source_json: str = "",
    source_text: str = "",
    source_id: str = "",
    source_url: str = "",
    max_records: int = 10,
) -> WebDataSchemaMappingResult:
    """Normalize retrieved structured or unstructured web data to a target schema."""

    schema_name, specs = _field_specs(
        _load_json(target_schema_json, field_name="target_schema_json")
    )
    if not specs:
        return WebDataSchemaMappingResult(
            status="invalid_schema",
            data_quality_issues=["target_schema_json must define a non-empty fields list."],
        )

    source_data = _load_json(source_json, field_name="source_json") if source_json.strip() else None
    source_type, source_records, issues = _records_from_source(source_data)
    bounded_max = max(1, min(int(max_records or 10), 25))
    records: list[StructuredWebRecord] = []
    unmapped: list[str] = []
    for record in source_records[:bounded_max]:
        mapped, record_unmapped = _map_record(
            record,
            specs=specs,
            source_id=source_id,
            source_url=source_url,
        )
        records.append(mapped)
        unmapped.extend(record_unmapped)

    if source_text.strip():
        if source_type == "structured_json":
            source_type = "mixed"
        else:
            source_type = "text"
        if not records:
            records.append(
                _record_from_text(
                    source_text,
                    specs=specs,
                    source_id=source_id,
                    source_url=source_url,
                )
            )

    if not records:
        issues.append("No source records or text were available to structure.")
    record_issues = [issue for record in records for issue in record.issues]
    status = (
        "success"
        if records and not issues and not record_issues
        else "partial"
        if records
        else "no_data"
    )
    return WebDataSchemaMappingResult(
        status=status,
        source_type=source_type,  # type: ignore[arg-type]
        target_schema_name=schema_name,
        fields=specs,
        records=records,
        unmapped_fields=sorted(set(unmapped))[:50],
        data_quality_issues=issues,
        send_enabled=False,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def structure_web_data_for_schema(
    target_schema_json: str,
    source_json: str = "",
    source_text: str = "",
    source_id: str = "",
    source_url: str = "",
    max_records: int = 10,
) -> str:
    """Structure retrieved web JSON/text into a bounded target schema for synthesis."""

    return _json_payload(
        structure_web_data_for_schema_impl(
            target_schema_json=target_schema_json,
            source_json=source_json,
            source_text=source_text,
            source_id=source_id,
            source_url=source_url,
            max_records=max_records,
        ).model_dump(mode="json")
    )
