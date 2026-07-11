"""Internal article, Airtable, and Google Workspace tools for Chief of Staff."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree

from keystone_agents.config import parse_bool
from keystone_agents.context_env import context_env_path, context_env_value
from keystone_agents.finance_expense_receipts import (
    FinanceReceiptEvidence,
    extract_finance_receipt_evidence,
    match_receipt_evidence_to_airtable_fields,
)
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.local_file_inputs import read_supported_local_file
from keystone_agents.memory import chief_of_staff_memory_item
from keystone_agents.schemas.airtable import (
    FINANCE_TAX_TRACKER_BASE_NAME,
    FINANCE_TAX_TRACKER_TABLES,
    AirtableBaseSchemaSummary,
    airtable_base_schema_summary_from_metadata,
    airtable_schema_context_markdown,
)
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    extract_website_content,
)

GOOGLE_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
DEFAULT_GOOGLE_DOCS_FOLDER = "KNIOps"
DEFAULT_GOOGLE_SHEETS_WORKBOOK = "KNIOps Structured Data"
DEFAULT_FINANCE_TAX_TRACKER_CONTEXT_DOC = "documents/finance_tax_tracker_context.md"
AIRTABLE_BASE_ALIAS_PREFIXES = {
    "finance_tax_tracker": "AIRTABLE_FINANCE_TAX_TRACKER",
    "finance": "AIRTABLE_FINANCE_TAX_TRACKER",
    "tax_tracker": "AIRTABLE_FINANCE_TAX_TRACKER",
    "kni_ops": "AIRTABLE_KNI_OPS",
    "kniops": "AIRTABLE_KNI_OPS",
    "ops": "AIRTABLE_KNI_OPS",
}
AIRTABLE_LIVE_READS_ENV = "KEYSTONE_AIRTABLE_LIVE_READS"
AIRTABLE_OPERATOR_APPROVAL_ENV = "KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE"
AIRTABLE_TEST_RECORD_MARKER = "KBA_TEST_RECORD"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME_TYPE = "application/vnd.google-apps.presentation"
POWERPOINT_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
DEFAULT_GOOGLE_SHEET_TABS = [
    "Contacts",
    "Companies",
    "Meetings",
    "FollowUps",
    "ChannelSummaries",
    "BudgetResources",
]
GOOGLE_WORKSPACE_TOOL_NAMES: tuple[str, ...] = (
    "google_doc_read",
    "google_doc_write",
    "google_doc_trash",
    "google_drive_list_folder",
    "google_drive_search_files",
    "google_drive_get_file_metadata",
    "google_slide_deck_read",
    "presentation_search_local",
    "presentation_read_local",
    "presentation_extract_slide_copy_local",
    "presentation_delete_test_artifact_local",
    "google_drive_create_folder",
    "google_drive_rename_folder",
    "google_drive_remove_folder",
    "google_sheet_list",
    "google_sheet_create",
    "google_sheet_read_table",
    "google_sheet_append_rows",
    "google_sheet_update_row",
    "google_sheet_delete_rows",
    "google_sheet_create_tab",
    "google_sheet_update_tab",
    "google_sheet_remove_tab",
    "google_sheet_trash",
)
GOOGLE_WORKSPACE_LIVE_READS_ENV = "KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS"


def _airtable_live_reads_default() -> bool:
    return parse_bool(os.getenv(AIRTABLE_LIVE_READS_ENV))


def _google_workspace_live_reads_default() -> bool:
    return parse_bool(os.getenv(GOOGLE_WORKSPACE_LIVE_READS_ENV))


def google_workspace_tools() -> list[Any]:
    """Return scoped Google Drive, Docs, and Sheets tools for agent builders."""

    return [
        google_doc_read,
        google_doc_write,
        google_doc_trash,
        google_drive_list_folder,
        google_drive_search_files,
        google_drive_get_file_metadata,
        google_slide_deck_read,
        presentation_search_local,
        presentation_read_local,
        presentation_extract_slide_copy_local,
        presentation_delete_test_artifact_local,
        google_drive_create_folder,
        google_drive_rename_folder,
        google_drive_remove_folder,
        google_sheet_list,
        google_sheet_create,
        google_sheet_read_table,
        google_sheet_append_rows,
        google_sheet_update_row,
        google_sheet_delete_rows,
        google_sheet_create_tab,
        google_sheet_update_tab,
        google_sheet_remove_tab,
        google_sheet_trash,
    ]


def explicit_full_article_read_requested(text: str) -> bool:
    """Return true when the operator asks to read linked source content."""

    lowered = str(text or "").lower()
    has_source_target = any(
        marker in lowered for marker in ("article", "link", "url", "page", "source", "web")
    )
    if has_source_target and re.search(
        r"\b(?:read|extract|fetch|open)\s*/\s*(?:read|extract|fetch|open)\b"
        r"|\b(?:read|extract|fetch|open)\b.{0,80}\b(?:urls?|links?|sources?|pages?|articles?)\b"
        r"|\b(?:urls?|links?|sources?|pages?|articles?)\b.{0,80}\b(?:read|extract|fetch|open)\b",
        lowered,
        flags=re.S,
    ):
        return True
    explicit_markers = (
        "read the full",
        "read full",
        "full article",
        "full text",
        "open the article",
        "open article",
        "open the link",
        "open link",
        "fetch the article",
        "fetch article",
        "read the linked",
        "read linked",
        "read article",
        "read the article",
        "read articles",
        "read the source",
        "read source",
    )
    if has_source_target and any(marker in lowered for marker in explicit_markers):
        return True
    deep_source_markers = (
        "deeper search",
        "deeper read-only search",
        "deepened search",
        "deep search",
        "detailed search",
        "source-backed",
        "source backed",
        "source-aware",
        "source aware",
        "source data",
        "source links",
        "source urls",
        "summarizes the source",
        "summarize the source",
        "detailed summary",
        "detailed synthesis",
        "detailed brief",
        "readable brief",
        "fuller brief",
    )
    if has_source_target and any(marker in lowered for marker in deep_source_markers):
        return True
    return bool(
        has_source_target
        and re.search(r"\bdeep(?:er|ened)?\b.{0,40}\bsearch\b", lowered)
        and re.search(r"\b(?:synthesis|summary|summari[sz]e|brief|source data)\b", lowered)
    )


def _live_source_read_enabled_from_env() -> bool:
    raw_live_mode = os.getenv("KEYSTONE_LIVE_MODE")
    if raw_live_mode is None or not parse_bool(raw_live_mode):
        return False
    raw_dry_run = os.getenv("KEYSTONE_DRY_RUN")
    if raw_dry_run is not None and parse_bool(raw_dry_run):
        return False
    raw_live_research = os.getenv("KEYSTONE_ENABLE_LIVE_RESEARCH")
    if raw_live_research is None and raw_dry_run is None:
        return False
    if raw_live_research is not None and not parse_bool(raw_live_research):
        return False
    return True


def read_linked_article_impl(
    url: str,
    *,
    request_text: str = "",
    max_chars: int = 6000,
    live: bool = False,
) -> dict[str, Any]:
    """Read a linked article only when the request explicitly asks for full content."""

    if not explicit_full_article_read_requested(request_text):
        return {
            "status": "not_enabled",
            "reason": (
                "Full article reading is default-off and requires an explicit "
                "natural-language request."
            ),
            "url": _normalize_http_url(url),
            "send_enabled": False,
        }
    normalized_url = _normalize_http_url(url)
    bounded_chars = min(max(int(max_chars or 6000), 1000), 12000)
    live_enabled = bool(live) or _live_source_read_enabled_from_env()
    if not live_enabled:
        return {
            "status": "dry-run",
            "url": normalized_url,
            "max_chars": bounded_chars,
            "planned_provider": os.getenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura"),
            "send_enabled": False,
        }
    provider = os.getenv("KEYSTONE_WEBSITE_EXTRACTOR") or "trafilatura"
    try:
        result = extract_website_content(
            normalized_url,
            company_name="article",
            provider=provider,
            live=True,
        )
    except WebsiteExtractionError as exc:
        return {
            "status": "extraction_failed",
            "url": normalized_url,
            "provider": provider,
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "text_or_markdown": "",
            "claims": [],
            "send_enabled": False,
        }
    text = result.text_or_markdown[:bounded_chars]
    return {
        "status": result.status,
        "url": result.url,
        "title": result.title,
        "provider": result.provider,
        "text_or_markdown": text,
        "truncated": len(result.text_or_markdown) > bounded_chars,
        "claims": result.claims,
        "metadata": _safe_metadata(result.metadata),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def read_linked_article(
    url: str,
    request_text: str = "",
    max_chars: int = 6000,
    live: bool = False,
) -> str:
    """Extract linked source text for explicit full-read or deep source-backed requests."""

    return json.dumps(
        read_linked_article_impl(
            url,
            request_text=request_text,
            max_chars=max_chars,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_get_base_schema_impl(
    *,
    base_alias: str = "",
    base_id: str = "",
    base_name: str = "",
    live: bool = False,
    persist_memory: bool = False,
    write_context_doc: bool = False,
    database_url: str | None = None,
    context_doc_path: str = DEFAULT_FINANCE_TAX_TRACKER_CONTEXT_DOC,
) -> dict[str, Any]:
    """Read Airtable base metadata and return a bounded prompt-safe schema summary."""

    config = _airtable_base_config(
        base_alias=base_alias,
        base_id=base_id,
        base_name=base_name,
        default_allowed_tables=FINANCE_TAX_TRACKER_TABLES,
    )
    resolved_base_id = config["base_id"]
    resolved_base_name = config["base_name"] or FINANCE_TAX_TRACKER_BASE_NAME
    allowed_tables = config["allowed_tables"] or FINANCE_TAX_TRACKER_TABLES
    request = {
        "method": "GET",
        "url": _airtable_base_schema_url(resolved_base_id or "app_dry_run"),
        "params": {},
    }
    if not live:
        summary = AirtableBaseSchemaSummary(
            base_id=resolved_base_id or "app_dry_run",
            base_name=resolved_base_name,
            allowed_tables=list(allowed_tables),
            missing_allowed_tables=list(allowed_tables),
        )
        return {
            "status": "dry-run",
            "request": _safe_request_preview(request),
            "schema": summary.model_dump(mode="json"),
            "persist_memory_requested": persist_memory,
            "write_context_doc_requested": write_context_doc,
            "send_enabled": False,
            "audit_notes": [
                "No Airtable metadata API call was made.",
                "Set AIRTABLE_BASE_ID and AIRTABLE_ACCESS_TOKEN, then call with live=true.",
            ],
        }

    _require_airtable_credentials(base_id=resolved_base_id, access_token=config["access_token"])
    payload = _airtable_send(request, access_token=config["access_token"])
    summary = airtable_base_schema_summary_from_metadata(
        payload,
        base_id=resolved_base_id,
        base_name=resolved_base_name,
        allowed_tables=allowed_tables,
    )
    result: dict[str, Any] = {
        "status": "success",
        "schema": summary.model_dump(mode="json"),
        "send_enabled": False,
        "audit_notes": [
            "Airtable metadata was reduced to bounded table and field summaries.",
            "Raw financial records were not stored in memory or local context docs.",
        ],
    }
    if persist_memory:
        result["memory_id"] = _save_airtable_schema_memory(summary, database_url=database_url)
    if write_context_doc:
        result["context_doc_path"] = str(
            _write_airtable_schema_context_doc(summary, context_doc_path)
        )
    return result


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_get_base_schema(
    base_alias: str = "",
    base_id: str = "",
    base_name: str = "",
    live: bool = False,
    persist_memory: bool = False,
    write_context_doc: bool = False,
    database_url: str = "",
    context_doc_path: str = DEFAULT_FINANCE_TAX_TRACKER_CONTEXT_DOC,
) -> str:
    """Read the configured Airtable base schema for bounded Chief of Staff context."""

    return json.dumps(
        airtable_get_base_schema_impl(
            base_alias=base_alias,
            base_id=base_id,
            base_name=base_name,
            live=live or _airtable_live_reads_default(),
            persist_memory=persist_memory,
            write_context_doc=write_context_doc,
            database_url=database_url or None,
            context_doc_path=context_doc_path,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_read_records_impl(
    table: str = "",
    *,
    base_alias: str = "",
    base_id: str = "",
    view: str = "",
    filter_formula: str = "",
    max_records: int = 0,
    fetch_all: bool = False,
    live: bool = False,
) -> dict[str, Any]:
    """Read Airtable records through the same env-backed config shape as Keystone Slack."""

    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    record_limit = int(max_records or 0)
    if fetch_all:
        record_limit = (
            record_limit
            if record_limit > 0
            else _airtable_int_env(
                "AIRTABLE_READ_ALL_MAX_RECORDS",
                5000,
            )
        )
        params: dict[str, str | int] = {"pageSize": 100}
    else:
        record_limit = min(max(record_limit or 10, 1), 50)
        params = {"maxRecords": record_limit}
    configured_view = (
        config["default_view"]
        if table_name == str(config.get("default_table") or "").strip()
        else ""
    )
    if view or configured_view:
        params["view"] = view.strip() or configured_view
    if filter_formula:
        params["filterByFormula"] = filter_formula
    request = {
        "method": "GET",
        "url": _airtable_table_url(table_name, base_id=config["base_id"]),
        "table": table_name,
        "params": params,
    }
    if not live:
        return {
            "status": "dry-run",
            "request": _safe_request_preview(request),
            "records": [],
            "fetch_all": fetch_all,
            "record_limit": record_limit,
            "send_enabled": False,
        }
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])
    if fetch_all:
        records: list[Any] = []
        offset = ""
        page_count = 0
        truncated = False
        while True:
            page_request = {
                **request,
                "params": {**params, **({"offset": offset} if offset else {})},
            }
            payload = _airtable_send(page_request, access_token=config["access_token"])
            page_records = payload.get("records", []) if isinstance(payload, dict) else []
            if isinstance(page_records, list):
                remaining = max(record_limit - len(records), 0)
                records.extend(page_records[:remaining])
            page_count += 1
            if len(records) >= record_limit:
                truncated = bool(
                    isinstance(payload, dict)
                    and (payload.get("offset") or len(page_records) >= remaining)
                )
                break
            offset = str(payload.get("offset") or "") if isinstance(payload, dict) else ""
            if not offset:
                break
        return {
            "status": "success",
            "table": table_name,
            "records": records,
            "fetch_all": True,
            "page_count": page_count,
            "record_limit": record_limit,
            "truncated": truncated,
            "send_enabled": False,
        }
    payload = _airtable_send(request, access_token=config["access_token"])
    return {
        "status": "success",
        "table": table_name,
        "records": payload.get("records", []) if isinstance(payload, dict) else [],
        "fetch_all": False,
        "record_limit": record_limit,
        "truncated": False,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_read_records(
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    view: str = "",
    filter_formula: str = "",
    max_records: int = 0,
    fetch_all: bool = False,
    live: bool = False,
) -> str:
    """Read approved Airtable records for internal Chief of Staff context."""

    return json.dumps(
        airtable_read_records_impl(
            table,
            base_alias=base_alias,
            base_id=base_id,
            view=view,
            filter_formula=filter_formula,
            max_records=max_records,
            fetch_all=fetch_all,
            live=live or _airtable_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_write_record_impl(
    fields_json: str,
    *,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    approval_reference: str = "",
    operation: str = "create",
    match_filter_formula: str = "",
    validate_schema: bool = False,
    live: bool = False,
) -> dict[str, Any]:
    """Create or update Airtable records behind explicit approval and env gates."""

    fields = _json_object(fields_json, "fields_json")
    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    clean_record_id = record_id.strip()
    clean_operation = str(operation or "create").strip().lower()
    clean_match_filter = match_filter_formula.strip()
    provider_fields = fields
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
    schema_validation: dict[str, Any] = {}
    if validate_schema:
        schema = airtable_get_base_schema_impl(
            base_alias=config["base_alias"] or base_alias,
            base_id=config["base_id"],
            live=live or _airtable_live_reads_default(),
        )
        schema_fields = _airtable_schema_fields_for_table(schema, table_name)
        if not schema_fields:
            return {
                "status": "blocked",
                "reason": "Airtable schema fields were unavailable for typed write validation.",
                "table": table_name,
                "send_enabled": False,
            }
        fields, field_errors = _coerce_airtable_write_fields(schema_fields, fields)
        schema_validation = {
            "validated": not field_errors,
            "field_types": {
                str(field.get("name") or ""): str(field.get("field_type") or "")
                for field in schema_fields
                if str(field.get("name") or "") in fields
            },
            "errors": field_errors,
        }
        if field_errors:
            return {
                "status": "blocked",
                "reason": "One or more Airtable fields failed schema-aware validation.",
                "table": table_name,
                "schema_validation": schema_validation,
                "send_enabled": False,
            }
        fields_by_name = {
            str(field.get("name") or ""): field
            for field in schema_fields
            if str(field.get("name") or "")
        }
        provider_fields = {
            str(fields_by_name[field_name].get("field_id") or field_name): value
            for field_name, value in fields.items()
        }
        schema_validation["provider_field_ids_used"] = any(
            provider_key != field_name
            for provider_key, field_name in zip(provider_fields, fields, strict=True)
        )
    if clean_operation == "update" and not clean_record_id:
        if not clean_match_filter:
            return {
                "status": "blocked",
                "reason": (
                    "Airtable updates require record_id or a deterministic match_filter_formula."
                ),
                "table": table_name,
                "send_enabled": False,
            }
        if not live:
            return {
                "status": "dry-run",
                "request": {
                    "method": "PATCH",
                    "table": table_name,
                    "match_filter_formula": clean_match_filter,
                    "payload": {"fields": fields},
                },
                "approval_reference": approval_reference.strip(),
                "send_enabled": False,
                "audit_notes": [
                    "Dry-run deterministic Airtable update preview only.",
                    "Live execution will proceed only if the match finds exactly one record.",
                ],
            }
        matches = airtable_read_records_impl(
            table_name,
            base_alias=base_alias,
            base_id=config["base_id"],
            filter_formula=clean_match_filter,
            max_records=2,
            live=True,
        ).get("records", [])
        if len(matches) != 1:
            return {
                "status": "blocked",
                "reason": "Airtable update match was not deterministic.",
                "table": table_name,
                "records_found": len(matches),
                "send_enabled": False,
            }
        clean_record_id = str(matches[0].get("id", "")).strip()
        if not clean_record_id:
            return {
                "status": "blocked",
                "reason": "Matched Airtable record did not include an id.",
                "table": table_name,
                "send_enabled": False,
            }
    method = "PATCH" if clean_record_id else "POST"
    url = _airtable_table_url(table_name, base_id=config["base_id"])
    if clean_record_id:
        url = f"{url}/{quote(clean_record_id, safe='')}"
    request = {
        "method": method,
        "url": url,
        "table": table_name,
        "params": (
            {"returnFieldsByFieldId": "true"}
            if schema_validation.get("provider_field_ids_used")
            else {}
        ),
        "payload": {"fields": provider_fields},
    }
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if dry_run:
        return {
            "status": "dry-run",
            "request": _safe_request_preview(request),
            "approval_reference": approval_reference.strip(),
            "schema_validation": schema_validation,
            "send_enabled": False,
        }
    if not approval_reference.strip():
        raise RuntimeError("Airtable live writes require a non-empty approval_reference.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
        raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])
    payload = _airtable_send(request, access_token=config["access_token"])
    written_record_id = str(payload.get("id") or clean_record_id).strip()
    verified_records: list[dict[str, Any]] = []
    if written_record_id:
        verified = airtable_read_records_impl(
            table_name,
            base_alias=base_alias,
            base_id=config["base_id"],
            filter_formula=f"RECORD_ID()='{written_record_id}'",
            max_records=1,
            live=True,
        )
        raw_verified_records = verified.get("records", [])
        if isinstance(raw_verified_records, list):
            verified_records = [
                record for record in raw_verified_records if isinstance(record, dict)
            ]
    verified_record = verified_records[0] if verified_records else {}
    verified_fields = verified_record.get("fields", {})
    if not isinstance(verified_fields, dict):
        verified_fields = {}
    matched_fields: list[str] = []
    for field_name, expected_value in fields.items():
        field_found, observed_value = _airtable_verified_field_value(
            verified_fields,
            field_name,
        )
        if (field_found and observed_value == expected_value) or (
            expected_value is False and not field_found
        ):
            matched_fields.append(field_name)
    mismatched_fields = sorted(set(fields) - set(matched_fields))
    verification = {
        "status": "verified" if not mismatched_fields else "verification_failed",
        "passed": bool(verified_record) and not mismatched_fields,
        "record_id_match": str(verified_record.get("id") or "") == written_record_id,
        "matched_fields": sorted(matched_fields),
        "mismatched_fields": mismatched_fields,
    }
    return {
        "status": "success",
        "operation": "update" if clean_record_id else "create",
        "table": table_name,
        "record_id": written_record_id,
        "record": payload,
        "verified_record": verified_record,
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "schema_validation": schema_validation,
        "send_enabled": False,
        "audit_notes": [
            "Airtable live write completed.",
            (
                "Read-after-write verification matched all requested fields."
                if verification["passed"]
                else "Read-after-write verification did not match all requested fields."
            ),
        ],
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_write_record(
    fields_json: str,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    approval_reference: str = "",
    operation: str = "create",
    match_filter_formula: str = "",
    validate_schema: bool = False,
    live: bool = False,
) -> str:
    """Create or update an approved Airtable record for internal review data."""

    return json.dumps(
        airtable_write_record_impl(
            fields_json,
            table=table,
            base_alias=base_alias,
            base_id=base_id,
            record_id=record_id,
            approval_reference=approval_reference,
            operation=operation,
            match_filter_formula=match_filter_formula,
            validate_schema=validate_schema,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_delete_test_record_impl(
    record_id: str,
    *,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one provider-verified disposable Airtable test record."""

    clean_record_id = record_id.strip()
    if not clean_record_id:
        raise ValueError("Airtable test-record deletion requires an exact record_id.")
    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    request = {
        "method": "DELETE",
        "url": (
            f"{_airtable_table_url(table_name, base_id=config['base_id'])}/"
            f"{quote(clean_record_id, safe='')}"
        ),
        "table": table_name,
        "params": {},
    }
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if dry_run:
        return {
            "status": "dry-run",
            "operation": "delete_test_record",
            "record_id": clean_record_id,
            "request": _safe_request_preview(request),
            "required_marker": AIRTABLE_TEST_RECORD_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "audit_notes": [
                "No Airtable record was deleted.",
                "Live deletion requires provider read-back of the disposable test marker.",
            ],
        }
    if not approval_reference.strip():
        raise RuntimeError("Airtable live test deletion requires a non-empty approval_reference.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
        raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_TEST_DELETES")):
        raise RuntimeError(
            "Airtable test-record deletion is disabled. "
            "Set AIRTABLE_ALLOW_TEST_DELETES=true for the approved cleanup window."
        )
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])

    before_result = airtable_read_records_impl(
        table_name,
        base_alias=base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=2,
        live=True,
    )
    before_records = before_result.get("records", [])
    if not isinstance(before_records, list) or len(before_records) != 1:
        return {
            "status": "blocked",
            "operation": "delete_test_record",
            "record_id": clean_record_id,
            "reason": "Exact Airtable test record could not be resolved uniquely.",
            "records_found": len(before_records) if isinstance(before_records, list) else 0,
            "required_marker": AIRTABLE_TEST_RECORD_MARKER,
            "send_enabled": False,
        }
    before_record = before_records[0]
    before_fields = before_record.get("fields", {}) if isinstance(before_record, dict) else {}
    if not _contains_airtable_test_marker(before_fields):
        return {
            "status": "blocked",
            "operation": "delete_test_record",
            "record_id": clean_record_id,
            "reason": "Resolved Airtable record does not contain the required test marker.",
            "required_marker": AIRTABLE_TEST_RECORD_MARKER,
            "send_enabled": False,
        }

    payload = _airtable_send(request, access_token=config["access_token"])
    after_result = airtable_read_records_impl(
        table_name,
        base_alias=base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=1,
        live=True,
    )
    after_records = after_result.get("records", [])
    provider_deleted = bool(payload.get("deleted")) if isinstance(payload, dict) else False
    absent_after = isinstance(after_records, list) and not after_records
    passed = provider_deleted and absent_after
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "delete_test_record",
        "table": table_name,
        "record_id": clean_record_id,
        "required_marker": AIRTABLE_TEST_RECORD_MARKER,
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "provider_deleted": provider_deleted,
            "record_absent_after": absent_after,
        },
        "send_enabled": False,
        "audit_notes": [
            "Provider read-back proved the disposable test marker before deletion.",
            (
                "Provider read-back confirmed the test record was removed."
                if passed
                else "Provider read-back did not confirm complete test-record removal."
            ),
        ],
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_delete_test_record(
    record_id: str,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved Airtable record only when it contains KBA_TEST_RECORD."""

    return json.dumps(
        airtable_delete_test_record_impl(
            record_id,
            table=table,
            base_alias=base_alias,
            base_id=base_id,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_test_record_lifecycle_impl(
    *,
    table: str = "Business Expenses",
    base_alias: str = "finance_tax_tracker",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create, verify, update, verify, and remove one marked test record."""

    clean_table = " ".join(str(table or "").split())
    if clean_table != "Business Expenses":
        raise ValueError(
            "The bounded Airtable test lifecycle supports only Business Expenses."
        )
    clean_approval = str(
        approval_reference or os.getenv(AIRTABLE_OPERATOR_APPROVAL_ENV, "")
    ).strip()
    if live and not clean_approval:
        raise RuntimeError(
            "The Airtable test lifecycle requires a non-empty approval_reference."
        )

    marker = f"{AIRTABLE_TEST_RECORD_MARKER} {uuid4().hex[:10]}"
    create_fields = {
        "Item": marker,
        "Description": f"{marker} created for delegated lifecycle validation",
    }
    update_fields = {
        "Description": f"{marker} updated and ready for verified cleanup",
    }
    create_result: dict[str, Any] = {}
    update_result: dict[str, Any] = {}
    delete_result: dict[str, Any] = {}
    record_id = ""
    failure = ""

    try:
        create_result = airtable_write_record_impl(
            json.dumps(create_fields),
            table=clean_table,
            base_alias=base_alias,
            approval_reference=f"{clean_approval}:create" if clean_approval else "",
            operation="create",
            validate_schema=live,
            live=live,
        )
        record_id = str(create_result.get("record_id") or "").strip()
        if not live:
            return {
                "status": "dry-run",
                "operation": "test_record_lifecycle",
                "table": clean_table,
                "required_marker": AIRTABLE_TEST_RECORD_MARKER,
                "approval_reference": clean_approval,
                "create": _airtable_lifecycle_step_receipt(create_result),
                "send_enabled": False,
            }
        if not record_id or not _airtable_verification_passed(create_result):
            failure = "Airtable test create did not pass provider read-back verification."
        else:
            update_result = airtable_write_record_impl(
                json.dumps(update_fields),
                table=clean_table,
                base_alias=base_alias,
                record_id=record_id,
                approval_reference=f"{clean_approval}:update",
                operation="update",
                validate_schema=True,
                live=True,
            )
            if not _airtable_verification_passed(update_result):
                failure = "Airtable test update did not pass provider read-back verification."
    except Exception as exc:  # cleanup is more important than propagating provider detail
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and record_id:
            try:
                delete_result = airtable_delete_test_record_impl(
                    record_id,
                    table=clean_table,
                    base_alias=base_alias,
                    approval_reference=f"{clean_approval}:delete",
                    live=True,
                )
            except Exception as exc:  # preserve cleanup failure in the bounded receipt
                delete_result = {
                    "status": "failed",
                    "operation": "delete_test_record",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "verification": {"passed": False},
                    "send_enabled": False,
                }

    create_passed = _airtable_verification_passed(create_result)
    update_passed = _airtable_verification_passed(update_result)
    cleanup_passed = _airtable_verification_passed(delete_result)
    passed = create_passed and update_passed and cleanup_passed and not failure
    return {
        "status": "success" if passed else "failed",
        "operation": "test_record_lifecycle",
        "table": clean_table,
        "record_id": record_id,
        "required_marker": AIRTABLE_TEST_RECORD_MARKER,
        "approval_reference": clean_approval,
        "create": _airtable_lifecycle_step_receipt(create_result),
        "update": _airtable_lifecycle_step_receipt(update_result),
        "delete": _airtable_lifecycle_step_receipt(delete_result),
        "verification": {
            "passed": passed,
            "create_read_back": create_passed,
            "same_record_update_read_back": update_passed,
            "record_absent_after_cleanup": cleanup_passed,
        },
        "failure": failure,
        "send_enabled": False,
    }


def _airtable_verification_passed(result: Mapping[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, Mapping) and verification.get("passed"))


def _airtable_lifecycle_step_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "table",
            "record_id",
            "required_marker",
            "verification",
            "send_enabled",
            "audit_notes",
            "reason",
        )
        if key in result
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_test_record_lifecycle(
    table: str = "Business Expenses",
    base_alias: str = "finance_tax_tracker",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Run one approved KBA_TEST_RECORD create/update/delete lifecycle with read-backs."""

    return json.dumps(
        airtable_test_record_lifecycle_impl(
            table=table,
            base_alias=base_alias,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _contains_airtable_test_marker(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_airtable_test_marker(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_airtable_test_marker(item) for item in value)
    return AIRTABLE_TEST_RECORD_MARKER.lower() in str(value or "").lower()


def airtable_upload_attachment_impl(
    local_file_path: str,
    *,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    field_id: str = "",
    field_name: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Upload an approved local PDF/image to an Airtable attachment field."""

    clean_record_id = record_id.strip()
    clean_field_id = field_id.strip()
    clean_field_name = field_name.strip()
    if not clean_record_id:
        raise ValueError("Airtable attachment uploads require record_id.")
    if not clean_field_id and not clean_field_name:
        raise ValueError("Airtable attachment uploads require field_id or field_name.")
    local_file = read_supported_local_file(local_file_path)
    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    if not clean_field_id and clean_field_name:
        schema = airtable_get_base_schema_impl(
            base_alias=config["base_alias"],
            base_id=config["base_id"],
            live=live or _airtable_live_reads_default(),
        )
        tables = schema.get("schema", {}).get("tables", []) if isinstance(schema, Mapping) else []
        for schema_table in tables:
            if not isinstance(schema_table, Mapping) or schema_table.get("name") != table_name:
                continue
            for field in schema_table.get("fields", []):
                if not isinstance(field, Mapping):
                    continue
                if field.get("name") == clean_field_name:
                    if str(field.get("field_type") or "") != "multipleAttachments":
                        raise ValueError(
                            f"Airtable field '{clean_field_name}' is not an attachment field."
                        )
                    clean_field_id = str(field.get("field_id") or "").strip()
                    break
        if not clean_field_id:
            raise ValueError(
                f"Airtable attachment field '{clean_field_name}' was not found in {table_name}."
            )
    request = {
        "method": "POST",
        "url": (
            "https://content.airtable.com/v0/"
            f"{quote(config['base_id'] or 'app_dry_run', safe='')}/"
            f"{quote(clean_record_id, safe='')}/"
            f"{quote(clean_field_id, safe='')}/uploadAttachment"
        ),
        "table": table_name,
        "params": {},
        "payload": {
            "contentType": local_file.mime_type,
            "filename": local_file.filename,
            "file": base64.b64encode(local_file.data).decode("ascii"),
        },
    }
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if dry_run:
        preview = _safe_request_preview(request)
        payload = dict(preview.get("payload", {}))
        payload["file"] = f"<base64 {local_file.size_bytes} bytes>"
        preview["payload"] = payload
        return {
            "status": "dry-run",
            "request": preview,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    if not approval_reference.strip():
        raise RuntimeError("Airtable attachment uploads require a non-empty approval_reference.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
        raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS")):
        raise RuntimeError(
            "Airtable attachment uploads are disabled. Set AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true."
        )
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])
    before = airtable_read_records_impl(
        table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=1,
        live=True,
    )
    before_records = before.get("records", [])
    if not isinstance(before_records, list) or len(before_records) != 1:
        return {
            "status": "blocked",
            "reason": "Exact Airtable record could not be resolved for attachment upload.",
            "record_id": clean_record_id,
            "send_enabled": False,
        }
    before_fields = before_records[0].get("fields", {})
    before_attachments = (
        before_fields.get(clean_field_name, [])
        if isinstance(before_fields, Mapping) and clean_field_name
        else []
    )
    before_count = len(before_attachments) if isinstance(before_attachments, list) else 0
    payload = _airtable_send(request, access_token=config["access_token"])
    after = airtable_read_records_impl(
        table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=1,
        live=True,
    )
    after_records = after.get("records", [])
    after_fields = (
        after_records[0].get("fields", {})
        if isinstance(after_records, list)
        and len(after_records) == 1
        and isinstance(after_records[0], Mapping)
        else {}
    )
    after_attachments = (
        after_fields.get(clean_field_name, [])
        if isinstance(after_fields, Mapping) and clean_field_name
        else []
    )
    matching = [
        item
        for item in after_attachments
        if isinstance(item, Mapping)
        and str(item.get("filename") or "") == local_file.filename
        and int(item.get("size") or 0) == local_file.size_bytes
    ] if isinstance(after_attachments, list) else []
    after_count = len(after_attachments) if isinstance(after_attachments, list) else 0
    verified = bool(
        clean_field_name
        and len(after_records) == 1
        and after_count == before_count + 1
        and len(matching) == 1
    )
    return {
        "status": "success" if verified else "verification_failed",
        "table": table_name,
        "record_id": clean_record_id,
        "field_id": clean_field_id,
        "field_name": clean_field_name,
        "filename": local_file.filename,
        "attachment": payload,
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if verified else "verification_failed",
            "passed": verified,
            "attachment_count_before": before_count,
            "attachment_count_after": after_count,
            "filename_match": len(matching) == 1,
            "size_match": len(matching) == 1,
        },
        "send_enabled": False,
        "audit_notes": [
            "Airtable attachment upload completed.",
            "Provider read-back verified the exact filename, byte size, and attachment count.",
        ],
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_upload_attachment(
    local_file_path: str,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    field_id: str = "",
    field_name: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Upload a readable local filesystem PDF/image; never use this tool for a URL.

    Use only when ``local_file_path`` is an actual local path supplied by the
    operator. For any credential-free HTTPS receipt URL, call
    ``airtable_link_attachment`` instead.
    """

    return json.dumps(
        airtable_upload_attachment_impl(
            local_file_path,
            table=table,
            base_alias=base_alias,
            base_id=base_id,
            record_id=record_id,
            field_id=field_id,
            field_name=field_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_link_attachment_impl(
    receipt_url: str,
    *,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    field_name: str = "Attachments",
    filename: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Append one HTTPS receipt link to an exact Airtable attachment field."""

    clean_record_id = record_id.strip()
    clean_url = receipt_url.strip()
    parsed_url = urlparse(clean_url)
    if not clean_record_id:
        raise ValueError("Airtable linked attachments require an exact record_id.")
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username:
        raise ValueError("Airtable receipt links require a credential-free HTTPS URL.")
    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    schema = airtable_get_base_schema_impl(
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        live=live or _airtable_live_reads_default(),
    )
    schema_fields = _airtable_schema_fields_for_table(schema, table_name)
    attachment_field = next(
        (
            field
            for field in schema_fields
            if str(field.get("name") or "") == field_name
            and str(field.get("field_type") or "") == "multipleAttachments"
        ),
        None,
    )
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if attachment_field is None:
        if dry_run and not schema_fields:
            return {
                "status": "dry-run",
                "operation": "link_attachment",
                "table": table_name,
                "record_id": clean_record_id,
                "field_name": field_name,
                "receipt_url_supplied": True,
                "filename": filename.strip(),
                "schema_validation": "pending_live_schema",
                "approval_reference": approval_reference.strip(),
                "send_enabled": False,
            }
        return {
            "status": "blocked",
            "reason": f"`{field_name}` is not a multipleAttachments field in {table_name}.",
            "table": table_name,
            "record_id": clean_record_id,
            "send_enabled": False,
        }
    if dry_run:
        return {
            "status": "dry-run",
            "operation": "link_attachment",
            "table": table_name,
            "record_id": clean_record_id,
            "field_name": field_name,
            "receipt_url_supplied": True,
            "filename": filename.strip(),
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    if not approval_reference.strip():
        raise RuntimeError("Airtable linked attachments require approval_reference.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
        raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS")):
        raise RuntimeError(
            "Airtable attachment links are disabled. Set "
            "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true."
        )
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])
    before = airtable_read_records_impl(
        table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=1,
        live=True,
    )
    records = before.get("records", [])
    if not isinstance(records, list) or len(records) != 1:
        return {
            "status": "blocked",
            "reason": "Exact Airtable record could not be resolved for attachment linking.",
            "record_id": clean_record_id,
            "send_enabled": False,
        }
    current = records[0].get("fields", {}).get(field_name, [])
    current_attachments = current if isinstance(current, list) else []
    preserved = [
        {"id": str(item.get("id"))}
        for item in current_attachments
        if isinstance(item, Mapping) and str(item.get("id") or "")
    ]
    linked = {"url": clean_url}
    if filename.strip():
        linked["filename"] = filename.strip()
    request = {
        "method": "PATCH",
        "url": (
            f"{_airtable_table_url(table_name, base_id=config['base_id'])}/"
            f"{quote(clean_record_id, safe='')}"
        ),
        "table": table_name,
        "params": {},
        "payload": {"fields": {field_name: [*preserved, linked]}},
    }
    payload = _airtable_send(request, access_token=config["access_token"])
    after = airtable_read_records_impl(
        table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        filter_formula=f"RECORD_ID()='{clean_record_id}'",
        max_records=1,
        live=True,
    )
    after_records = after.get("records", [])
    after_attachments = (
        after_records[0].get("fields", {}).get(field_name, [])
        if isinstance(after_records, list) and len(after_records) == 1
        else []
    )
    passed = isinstance(after_attachments, list) and len(after_attachments) > len(
        current_attachments
    )
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "link_attachment",
        "table": table_name,
        "record_id": clean_record_id,
        "field_name": field_name,
        "filename": filename.strip(),
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "attachment_count_before": len(current_attachments),
            "attachment_count_after": len(after_attachments)
            if isinstance(after_attachments, list)
            else 0,
        },
        "provider_record_id": str(payload.get("id") or "") if isinstance(payload, Mapping) else "",
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_link_attachment(
    receipt_url: str,
    table: str = "",
    base_alias: str = "",
    base_id: str = "",
    record_id: str = "",
    field_name: str = "Attachments",
    filename: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Attach a credential-free HTTPS receipt URL; never treat it as a local file path.

    Use this tool, not ``airtable_upload_attachment``, whenever the supplied
    receipt begins with ``https://``. Airtable fetches the URL into the exact
    attachment field and the tool verifies the attachment-count increase.
    """

    return json.dumps(
        airtable_link_attachment_impl(
            receipt_url,
            table=table,
            base_alias=base_alias,
            base_id=base_id,
            record_id=record_id,
            field_name=field_name,
            filename=filename,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def airtable_create_expense_from_receipt_impl(
    local_file_path: str,
    *,
    table: str = "",
    base_alias: str = "finance_tax_tracker",
    base_id: str = "",
    receipt_fields_json: str = "",
    field_values_json: str = "",
    category: str = "",
    payment_method: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create one finance tracker expense record from a receipt, then attach it."""

    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
        default_allowed_tables=FINANCE_TAX_TRACKER_TABLES,
    )
    table_name = _airtable_table(table, config=config)
    if table_name not in {"Business Expenses", "Personal Expenses"}:
        return {
            "status": "blocked",
            "reason": "Receipt expense creates are limited to Business Expenses or Personal Expenses.",
            "table": table_name,
            "send_enabled": False,
        }
    extracted_evidence = extract_finance_receipt_evidence(local_file_path)
    model_evidence = _model_receipt_evidence_from_json(
        receipt_fields_json,
        source_path=local_file_path,
    )
    evidence, evidence_notes = _merge_receipt_evidence(
        extracted_evidence,
        model_evidence,
    )
    if not evidence.content_read:
        return {
            "status": "blocked",
            "reason": (
                "Receipt content could not be read by the model or deterministic extraction; "
                "no Airtable write was attempted."
            ),
            "receipt_blocker": evidence.blocker,
            "table": table_name,
            "send_enabled": False,
        }
    conflict_notes = [note for note in evidence_notes if note.startswith("conflict:")]
    if conflict_notes:
        return {
            "status": "blocked",
            "reason": "Model-extracted receipt fields conflict with deterministic extraction.",
            "conflicts": conflict_notes,
            "table": table_name,
            "send_enabled": False,
        }
    schema = airtable_get_base_schema_impl(
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        live=live or _airtable_live_reads_default(),
    )
    schema_fields = _airtable_schema_fields_for_table(schema, table_name)
    if not schema_fields:
        return {
            "status": "blocked",
            "reason": "Airtable schema fields were unavailable for the target expense table.",
            "schema_status": schema.get("status") if isinstance(schema, Mapping) else "",
            "table": table_name,
            "send_enabled": False,
        }
    mapping = match_receipt_evidence_to_airtable_fields(evidence, schema_fields)
    fields = dict(mapping.get("fields") or {})
    field_by_name = {
        str(field.get("name") or ""): field
        for field in schema_fields
        if isinstance(field, Mapping) and str(field.get("name") or "")
    }
    optional_field_notes: list[str] = []
    _apply_schema_select_override(
        fields,
        field_by_name,
        "Categories",
        category,
        optional_field_notes,
    )
    _apply_schema_select_override(
        fields,
        field_by_name,
        "Payment Method",
        payment_method,
        optional_field_notes,
    )
    _apply_model_schema_field_values(
        fields,
        schema_fields,
        field_values_json,
        optional_field_notes,
    )
    if not fields:
        return {
            "status": "blocked",
            "reason": "No receipt-backed schema fields were available for the expense create.",
            "mapping": mapping,
            "send_enabled": False,
        }
    attachment_field = mapping.get("attachment_field") if isinstance(mapping, Mapping) else {}
    live_write_requested = live and not parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if live_write_requested:
        if not approval_reference.strip():
            raise RuntimeError("Airtable receipt expense creates require approval_reference.")
        if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
            raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
        if isinstance(attachment_field, Mapping) and attachment_field and not parse_bool(
            os.getenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS")
        ):
            raise RuntimeError(
                "Airtable attachment uploads are disabled. Set "
                "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true before creating receipt-backed expenses."
            )
        _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])
    write_result = airtable_write_record_impl(
        json.dumps(fields, ensure_ascii=True, sort_keys=True),
        table=table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        approval_reference=approval_reference,
        operation="create",
        live=live,
    )
    record_id = str(write_result.get("record_id") or "").strip()
    dry_run = write_result.get("status") == "dry-run"
    attachment_result: dict[str, Any] = {}
    if isinstance(attachment_field, Mapping) and attachment_field:
        attachment_record_id = record_id
        if dry_run and not attachment_record_id:
            attachment_record_id = "rec_dry_run_after_create"
        if attachment_record_id:
            attachment_result = airtable_upload_attachment_impl(
                local_file_path,
                table=table_name,
                base_alias=config["base_alias"] or base_alias,
                base_id=config["base_id"],
                record_id=attachment_record_id,
                field_id=str(attachment_field.get("field_id") or ""),
                field_name=str(attachment_field.get("name") or ""),
                approval_reference=approval_reference,
                live=live and bool(record_id),
            )
    status = (
        "success"
        if write_result.get("status") == "success"
        and attachment_result.get("status") in {"success", ""}
        else "dry-run"
        if dry_run
        else "partial"
    )
    return {
        "status": status,
        "table": table_name,
        "receipt_evidence": {
            "vendor": evidence.vendor,
            "receipt_date": evidence.receipt_date,
            "estimated_tax_periods": evidence.estimated_tax_periods,
            "order_number": evidence.order_number,
            "total": evidence.total,
            "currency": evidence.currency,
            "extraction_method": evidence.extraction_method,
        },
        "mapped_fields": fields,
        "mapping": mapping,
        "optional_field_notes": optional_field_notes,
        "evidence_notes": evidence_notes,
        "write_result": write_result,
        "attachment_result": attachment_result,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_create_expense_from_receipt(
    local_file_path: str,
    table: str = "",
    base_alias: str = "finance_tax_tracker",
    base_id: str = "",
    receipt_fields_json: str = "",
    field_values_json: str = "",
    category: str = "",
    payment_method: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create an approved finance tracker expense from a local receipt and attach it."""

    return json.dumps(
        airtable_create_expense_from_receipt_impl(
            local_file_path,
            table=table,
            base_alias=base_alias,
            base_id=base_id,
            receipt_fields_json=receipt_fields_json,
            field_values_json=field_values_json,
            category=category,
            payment_method=payment_method,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_doc_read_impl(
    document_id_or_url: str,
    *,
    folder_path: str = "",
    max_chars: int = 6000,
    live: bool = False,
) -> dict[str, Any]:
    """Read a Google Doc body through the configured OAuth token when live-enabled."""

    document_id = _google_doc_id(document_id_or_url)
    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_chars = min(max(int(max_chars or 6000), 1000), 12000)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_doc",
            "document_id": document_id,
            "folder_path": target_folder_path,
            "max_chars": bounded_chars,
            "send_enabled": False,
        }
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    _assert_drive_file_in_folder(services["drive"], document_id, target_folder_path)
    docs_service = services["docs"]
    document = docs_service.documents().get(documentId=document_id).execute()
    title = str(document.get("title", ""))
    text = _google_doc_text(document)[:bounded_chars]
    return {
        "status": "success",
        "operation": "read_doc",
        "document_id": document_id,
        "title": title,
        "text": text,
        "char_count": len(text),
        "truncated": len(_google_doc_text(document)) > bounded_chars,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_read(
    document_id_or_url: str,
    folder_path: str = "",
    max_chars: int = 6000,
    live: bool = False,
) -> str:
    """Read an approved Google Doc for internal Chief of Staff context."""

    return json.dumps(
        google_doc_read_impl(
            document_id_or_url,
            folder_path=folder_path,
            max_chars=max_chars,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_slide_deck_read_impl(
    presentation_id_or_url: str,
    *,
    folder_path: str = "",
    max_slides: int = 40,
    max_chars_per_slide: int = 4000,
    include_speaker_notes: bool = True,
    live: bool = False,
) -> dict[str, Any]:
    """Read slide text and notes without modifying or copying the parent deck."""

    presentation_id = _google_drive_file_id(presentation_id_or_url)
    if not presentation_id:
        raise ValueError("presentation_id_or_url is required.")
    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_slides = min(max(int(max_slides or 40), 1), 100)
    bounded_chars = min(max(int(max_chars_per_slide or 4000), 500), 12000)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_slide_deck",
            "presentation_id": presentation_id,
            "folder_path": target_folder_path,
            "max_slides": bounded_slides,
            "max_chars_per_slide": bounded_chars,
            "include_speaker_notes": bool(include_speaker_notes),
            "parent_modified": False,
            "send_enabled": False,
        }

    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    _assert_drive_file_in_folder(drive_service, presentation_id, target_folder_path)
    metadata = (
        drive_service.files()
        .get(
            fileId=presentation_id,
            fields="id,name,mimeType,modifiedTime,webViewLink,size,parents,trashed",
        )
        .execute()
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("Google Drive returned no presentation metadata.")
    mime_type = str(metadata.get("mimeType") or "")
    if mime_type == GOOGLE_SLIDES_MIME_TYPE:
        presentation = (
            services["slides"].presentations().get(presentationId=presentation_id).execute()
        )
        artifacts = _google_slides_artifacts(
            presentation,
            presentation_id=presentation_id,
            max_slides=bounded_slides,
            max_chars=bounded_chars,
            include_speaker_notes=include_speaker_notes,
        )
        identity_scope = "provider_slide_object_id"
        extraction_method = "google_slides_api"
        content_bytes = json.dumps(
            presentation, ensure_ascii=True, sort_keys=True, default=str
        ).encode("utf-8")
    elif mime_type == POWERPOINT_MIME_TYPE:
        size = int(metadata.get("size") or 0)
        if size > 25_000_000:
            raise RuntimeError("PowerPoint deck exceeds the 25 MB bounded read limit.")
        content = drive_service.files().get_media(fileId=presentation_id).execute()
        if not isinstance(content, bytes):
            raise RuntimeError("PowerPoint download returned an unexpected payload.")
        artifacts = _powerpoint_slide_artifacts(
            content,
            presentation_id=presentation_id,
            max_slides=bounded_slides,
            max_chars=bounded_chars,
            include_speaker_notes=include_speaker_notes,
        )
        identity_scope = "snapshot_slide_position"
        extraction_method = "powerpoint_open_xml"
        content_bytes = content
    else:
        raise RuntimeError(
            "The exact Workspace target is not a Google Slides or PowerPoint deck."
        )

    full_count = len(artifacts["all_slides"])
    slides = artifacts["all_slides"][:bounded_slides]
    return {
        "status": "success",
        "operation": "read_slide_deck",
        "presentation_id": presentation_id,
        "title": str(metadata.get("name") or ""),
        "mime_type": mime_type,
        "url": str(metadata.get("webViewLink") or ""),
        "modified_time": str(metadata.get("modifiedTime") or ""),
        "folder_path": target_folder_path,
        "slide_count": full_count,
        "returned_slide_count": len(slides),
        "truncated": full_count > len(slides),
        "slides": slides,
        "identity_scope": identity_scope,
        "extraction_method": extraction_method,
        "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "parent_modified": False,
        "derived_copy_created": False,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_slide_deck_read(
    presentation_id_or_url: str,
    folder_path: str = "",
    max_slides: int = 40,
    max_chars_per_slide: int = 4000,
    include_speaker_notes: bool = True,
    live: bool = False,
) -> str:
    """Read bounded Google Slides or PowerPoint text/notes without modifying the deck."""

    return json.dumps(
        google_slide_deck_read_impl(
            presentation_id_or_url,
            folder_path=folder_path,
            max_slides=max_slides,
            max_chars_per_slide=max_chars_per_slide,
            include_speaker_notes=include_speaker_notes,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _presentation_library_root() -> Path:
    raw = (
        context_env_value("KEYSTONE_PRESENTATION_LIBRARY_ROOT").strip()
        or context_env_value("KNI_CLINICAL_AI_SLIDES_ROOT").strip()
    )
    if not raw:
        raise RuntimeError(
            "Local presentation reads require KEYSTONE_PRESENTATION_LIBRARY_ROOT "
            "or KNI_CLINICAL_AI_SLIDES_ROOT."
        )
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("The configured local presentation library root is unavailable.")
    return root


def presentation_search_local_impl(
    query: str = "",
    *,
    max_items: int = 25,
    live: bool = False,
) -> dict[str, Any]:
    """Search an allowlisted local PowerPoint library without reading deck content."""

    clean_query = " ".join(str(query or "").split())
    bounded_items = min(max(int(max_items or 25), 1), 100)
    if not live:
        return {
            "status": "dry-run",
            "operation": "search_local_presentations",
            "query": clean_query,
            "max_items": bounded_items,
            "items": [],
            "item_count": 0,
            "parent_modified": False,
            "send_enabled": False,
        }
    root = _presentation_library_root()
    query_terms = [term.lower() for term in re.findall(r"[a-zA-Z0-9]+", clean_query)]
    candidates: list[tuple[int, float, Path]] = []
    scanned = 0
    for path in root.rglob("*.pptx"):
        if scanned >= 5000:
            break
        scanned += 1
        relative = path.relative_to(root)
        haystack = " ".join(relative.parts).lower()
        score = sum(1 for term in query_terms if term in haystack)
        if query_terms and score == 0:
            continue
        stat = path.stat()
        candidates.append((score, stat.st_mtime, path))
    candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    items = []
    for score, _modified, path in candidates[:bounded_items]:
        stat = path.stat()
        items.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "name": path.name,
                "mime_type": POWERPOINT_MIME_TYPE,
                "size": stat.st_size,
                "modified_time": datetime.fromtimestamp(
                    stat.st_mtime, tz=UTC
                ).isoformat(),
                "query_term_matches": score,
            }
        )
    return {
        "status": "success",
        "operation": "search_local_presentations",
        "query": clean_query,
        "items": items,
        "item_count": len(items),
        "scanned_count": scanned,
        "scan_truncated": scanned >= 5000,
        "parent_modified": False,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def presentation_search_local(
    query: str = "",
    max_items: int = 25,
    live: bool = False,
) -> str:
    """Search the configured local presentation library by title or relative path."""

    return json.dumps(
        presentation_search_local_impl(
            query,
            max_items=max_items,
            live=live or parse_bool(os.getenv("KEYSTONE_PRESENTATION_LIVE_READS")),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def presentation_read_local_impl(
    relative_path: str,
    *,
    max_slides: int = 40,
    max_chars_per_slide: int = 4000,
    include_speaker_notes: bool = True,
    live: bool = False,
) -> dict[str, Any]:
    """Read one allowlisted local PowerPoint deck without modifying the parent."""

    clean_relative = str(relative_path or "").strip()
    if not clean_relative:
        raise ValueError("relative_path is required.")
    bounded_slides = min(max(int(max_slides or 40), 1), 100)
    bounded_chars = min(max(int(max_chars_per_slide or 4000), 500), 12000)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_local_presentation",
            "relative_path": clean_relative,
            "max_slides": bounded_slides,
            "max_chars_per_slide": bounded_chars,
            "include_speaker_notes": bool(include_speaker_notes),
            "parent_modified": False,
            "send_enabled": False,
        }
    root = _presentation_library_root()
    candidate = Path(clean_relative).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError("Presentation target must stay inside the configured library root.")
    if not target.is_file() or target.suffix.lower() != ".pptx":
        raise RuntimeError("The exact local presentation target is not an available .pptx file.")
    if target.stat().st_size > 25_000_000:
        raise RuntimeError("PowerPoint deck exceeds the 25 MB bounded read limit.")
    content = target.read_bytes()
    artifacts = _powerpoint_slide_artifacts(
        content,
        presentation_id=hashlib.sha256(
            target.relative_to(root).as_posix().encode("utf-8")
        ).hexdigest()[:16],
        max_slides=bounded_slides,
        max_chars=bounded_chars,
        include_speaker_notes=include_speaker_notes,
    )
    all_slides = artifacts["all_slides"]
    slides = all_slides[:bounded_slides]
    stat = target.stat()
    return {
        "status": "success",
        "operation": "read_local_presentation",
        "relative_path": target.relative_to(root).as_posix(),
        "title": target.name,
        "mime_type": POWERPOINT_MIME_TYPE,
        "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        "size": stat.st_size,
        "slide_count": len(all_slides),
        "returned_slide_count": len(slides),
        "truncated": len(all_slides) > len(slides),
        "slides": slides,
        "identity_scope": "snapshot_slide_position",
        "extraction_method": "powerpoint_open_xml",
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "parent_modified": False,
        "derived_copy_created": False,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def presentation_read_local(
    relative_path: str,
    max_slides: int = 40,
    max_chars_per_slide: int = 4000,
    include_speaker_notes: bool = True,
    live: bool = False,
) -> str:
    """Read one bounded local PowerPoint deck using allowlisted relative provenance."""

    return json.dumps(
        presentation_read_local_impl(
            relative_path,
            max_slides=max_slides,
            max_chars_per_slide=max_chars_per_slide,
            include_speaker_notes=include_speaker_notes,
            live=live or parse_bool(os.getenv("KEYSTONE_PRESENTATION_LIVE_READS")),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _presentation_derived_root() -> Path:
    workspace = Path.cwd().resolve()
    configured = context_env_value(
        "KEYSTONE_PRESENTATION_DERIVED_ROOT", "artifacts/presentation-derived"
    ).strip()
    candidate = Path(configured).expanduser()
    root = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if root != workspace and workspace not in root.parents:
        raise RuntimeError("Presentation derived artifacts must stay inside the workspace.")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _require_presentation_derived_write(approval_reference: str) -> None:
    if not approval_reference.strip():
        raise RuntimeError("Derived presentation writes require a non-empty approval_reference.")
    if not parse_bool(os.getenv("KEYSTONE_PRESENTATION_ALLOW_DERIVED_WRITES")):
        raise RuntimeError(
            "Derived presentation writes are disabled. Set "
            "KEYSTONE_PRESENTATION_ALLOW_DERIVED_WRITES=true for the approved window."
        )


def _safe_derived_slide_name(value: str, *, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-._")
    if not cleaned:
        cleaned = "derived-slide"
    if not cleaned.lower().endswith(f".{suffix}"):
        cleaned = f"{cleaned}.{suffix}"
    return cleaned[:180]


def presentation_extract_slide_copy_local_impl(
    relative_path: str,
    slide_number: int,
    *,
    output_format: str = "png",
    output_name: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Render one slide to a derived PNG/PDF while proving the parent is unchanged."""

    clean_relative = str(relative_path or "").strip()
    if not clean_relative:
        raise ValueError("relative_path is required.")
    page = int(slide_number or 0)
    if page <= 0:
        raise ValueError("slide_number must be a positive integer.")
    clean_format = str(output_format or "png").strip().lower()
    if clean_format not in {"png", "pdf"}:
        raise ValueError("output_format must be 'png' or 'pdf'.")
    default_name = f"derived-slide-{page}"
    filename = _safe_derived_slide_name(output_name or default_name, suffix=clean_format)
    if not live:
        return {
            "status": "dry-run",
            "operation": "extract_slide_copy",
            "relative_path": clean_relative,
            "slide_number": page,
            "output_format": clean_format,
            "output_name": filename,
            "approval_reference": approval_reference.strip(),
            "parent_modified": False,
            "derived_copy_created": False,
            "send_enabled": False,
        }
    _require_presentation_derived_write(approval_reference)
    library_root = _presentation_library_root()
    source_candidate = Path(clean_relative).expanduser()
    source = (
        source_candidate.resolve()
        if source_candidate.is_absolute()
        else (library_root / source_candidate).resolve()
    )
    if source != library_root and library_root not in source.parents:
        raise RuntimeError("Presentation target must stay inside the configured library root.")
    if not source.is_file() or source.suffix.lower() != ".pptx":
        raise RuntimeError("The exact presentation source is not an available .pptx file.")
    source_bytes = source.read_bytes()
    source_hash_before = hashlib.sha256(source_bytes).hexdigest()
    source_mtime_before = source.stat().st_mtime_ns
    slide_count = len(
        _powerpoint_slide_artifacts(
            source_bytes,
            presentation_id="validation",
            max_slides=100,
            max_chars=500,
            include_speaker_notes=False,
        )["all_slides"]
    )
    if page > slide_count:
        raise RuntimeError(
            f"slide_number {page} exceeds the deck's {slide_count} slides."
        )
    output_root = _presentation_derived_root()
    target = (output_root / filename).resolve()
    if target.parent != output_root:
        raise RuntimeError("Derived presentation output must stay in the configured root.")
    if target.exists():
        raise RuntimeError("The exact derived presentation output already exists.")

    soffice = shutil.which("soffice")
    pdfseparate = shutil.which("pdfseparate")
    pdftoppm = shutil.which("pdftoppm")
    if not soffice or not pdfseparate or (clean_format == "png" and not pdftoppm):
        raise RuntimeError("Required local presentation rendering tools are unavailable.")
    with tempfile.TemporaryDirectory(prefix="kba-slide-render-") as temporary_dir:
        temporary = Path(temporary_dir)
        libreoffice_profile = temporary / "libreoffice-profile"
        _run_bounded_command(
            [
                soffice,
                f"-env:UserInstallation={libreoffice_profile.as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(temporary),
                str(source),
            ],
            timeout=120,
        )
        deck_pdf = temporary / f"{source.stem}.pdf"
        if not deck_pdf.is_file():
            raise RuntimeError("Presentation renderer did not produce the expected PDF.")
        page_pattern = temporary / "slide-%d.pdf"
        _run_bounded_command(
            [
                pdfseparate,
                "-f",
                str(page),
                "-l",
                str(page),
                str(deck_pdf),
                str(page_pattern),
            ],
            timeout=60,
        )
        page_pdf = temporary / f"slide-{page}.pdf"
        if not page_pdf.is_file():
            raise RuntimeError("PDF page extraction did not produce the requested slide.")
        if clean_format == "pdf":
            shutil.copyfile(page_pdf, target)
        else:
            png_stem = temporary / "slide"
            _run_bounded_command(
                [pdftoppm, "-png", "-singlefile", "-r", "150", str(page_pdf), str(png_stem)],
                timeout=60,
            )
            rendered_png = temporary / "slide.png"
            if not rendered_png.is_file():
                raise RuntimeError("PNG rendering did not produce the requested slide.")
            shutil.copyfile(rendered_png, target)

    source_hash_after = hashlib.sha256(source.read_bytes()).hexdigest()
    source_mtime_after = source.stat().st_mtime_ns
    parent_unchanged = bool(
        source_hash_after == source_hash_before and source_mtime_after == source_mtime_before
    )
    if not parent_unchanged:
        target.unlink(missing_ok=True)
        raise RuntimeError("Parent deck changed during derived slide extraction.")
    output_bytes = target.read_bytes()
    magic_valid = (
        output_bytes.startswith(b"%PDF")
        if clean_format == "pdf"
        else output_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    )
    if not output_bytes or not magic_valid:
        target.unlink(missing_ok=True)
        raise RuntimeError("Derived slide artifact failed file-signature verification.")
    workspace = Path.cwd().resolve()
    return {
        "status": "success",
        "operation": "extract_slide_copy",
        "relative_path": source.relative_to(library_root).as_posix(),
        "parent_title": source.name,
        "parent_content_sha256": source_hash_before,
        "slide_number": page,
        "slide_identity_scope": "snapshot_slide_position",
        "output_format": clean_format,
        "artifact_path": target.relative_to(workspace).as_posix(),
        "artifact_size": len(output_bytes),
        "artifact_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified",
            "passed": True,
            "artifact_exists": target.is_file(),
            "file_signature_valid": magic_valid,
            "parent_hash_match": source_hash_after == source_hash_before,
            "parent_mtime_match": source_mtime_after == source_mtime_before,
        },
        "parent_modified": False,
        "derived_copy_created": True,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def presentation_extract_slide_copy_local(
    relative_path: str,
    slide_number: int,
    output_format: str = "png",
    output_name: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Render one approved slide copy to PNG or PDF without modifying its parent deck."""

    return json.dumps(
        presentation_extract_slide_copy_local_impl(
            relative_path,
            slide_number,
            output_format=output_format,
            output_name=output_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def presentation_delete_test_artifact_local_impl(
    artifact_path: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one exact marked derived slide test artifact and verify absence."""

    clean_path = str(artifact_path or "").strip()
    if not clean_path:
        raise ValueError("artifact_path is required.")
    if "KBA_TEST_SLIDE" not in Path(clean_path).name:
        raise ValueError("Test artifact cleanup requires KBA_TEST_SLIDE in the filename.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "delete_test_slide_artifact",
            "artifact_path": clean_path,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
        }
    _require_presentation_derived_write(approval_reference)
    root = _presentation_derived_root()
    workspace = Path.cwd().resolve()
    candidate = Path(clean_path).expanduser()
    target = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if target.parent != root or target.suffix.lower() not in {".png", ".pdf"}:
        raise RuntimeError("Test artifact cleanup is limited to the derived presentation root.")
    if not target.is_file():
        raise RuntimeError("The exact marked derived slide artifact does not exist.")
    before_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    target.unlink()
    passed = not target.exists()
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "delete_test_slide_artifact",
        "artifact_path": target.relative_to(workspace).as_posix(),
        "artifact_sha256": before_hash,
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "artifact_absent_after": passed,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def presentation_delete_test_artifact_local(
    artifact_path: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved marked KBA_TEST_SLIDE artifact and verify absence."""

    return json.dumps(
        presentation_delete_test_artifact_local_impl(
            artifact_path,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _run_bounded_command(command: list[str], *, timeout: int) -> None:
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = " ".join(result.stderr.split())[-700:]
        raise RuntimeError(
            f"Local presentation command failed with exit {result.returncode}"
            + (f": {detail}" if detail else ".")
        )


def google_doc_write_impl(
    title: str,
    body_text: str,
    *,
    document_id: str = "",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create or replace a Google Doc body behind explicit approval and env gates."""

    cleaned_title = " ".join(str(title or "Keystone Chief of Staff Artifact").split())
    body = str(body_text or "").strip()
    target_folder_path = _google_docs_folder_path(folder_path)
    if not body:
        raise ValueError("body_text is required for Google Doc writes.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "write_doc",
            "title": cleaned_title,
            "document_id": document_id.strip(),
            "folder_path": target_folder_path,
            "body_preview": body[:1200],
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    docs_service = services["docs"]
    target_id = document_id.strip()
    if target_id:
        _assert_drive_file_in_folder(services["drive"], target_id, target_folder_path)
        docs_service.documents().batchUpdate(
            documentId=target_id,
            body={"requests": _replace_doc_requests(docs_service, target_id, body)},
        ).execute()
    else:
        created = docs_service.documents().create(body={"title": cleaned_title}).execute()
        target_id = str(created.get("documentId", ""))
        folder_id = _ensure_drive_folder_path(services["drive"], target_folder_path)
        if folder_id:
            _move_drive_file_to_folder(services["drive"], target_id, folder_id)
        docs_service.documents().batchUpdate(
            documentId=target_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]},
        ).execute()
    return {
        "status": "success",
        "document_id": target_id,
        "title": cleaned_title,
        "url": f"https://docs.google.com/document/d/{target_id}/edit",
        "folder_path": target_folder_path,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_write(
    title: str,
    body_text: str,
    document_id: str = "",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create or update an approved Google Doc internal artifact."""

    return json.dumps(
        google_doc_write_impl(
            title,
            body_text,
            document_id=document_id,
            folder_path=folder_path,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_doc_trash_impl(
    document_id_or_url: str,
    *,
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Move one exact scoped Google Doc to Drive trash with provider read-back."""

    document_id = _google_doc_id(document_id_or_url)
    target_folder_path = _google_docs_folder_path(folder_path)
    if not document_id:
        raise ValueError("document_id_or_url is required.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "trash_doc",
            "document_id": document_id,
            "folder_path": target_folder_path,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    _assert_drive_file_in_folder(drive_service, document_id, target_folder_path)
    existing = (
        drive_service.files()
        .get(fileId=document_id, fields="id,name,mimeType,trashed,webViewLink,parents")
        .execute()
    )
    if not isinstance(existing, dict) or existing.get("mimeType") != GOOGLE_DOC_MIME_TYPE:
        raise RuntimeError("The exact Workspace target is not a Google Doc.")
    metadata = (
        drive_service.files()
        .update(
            fileId=document_id,
            body={"trashed": True},
            fields="id,name,mimeType,trashed,webViewLink,parents",
        )
        .execute()
    )
    verified = (
        drive_service.files()
        .get(fileId=document_id, fields="id,name,mimeType,trashed,webViewLink,parents")
        .execute()
    )
    verification = {
        "status": "verified",
        "passed": bool(
            isinstance(verified, dict)
            and str(verified.get("id", "")) == document_id
            and verified.get("mimeType") == GOOGLE_DOC_MIME_TYPE
            and verified.get("trashed") is True
        ),
        "document_id_match": bool(
            isinstance(verified, dict) and str(verified.get("id", "")) == document_id
        ),
        "mime_type_match": bool(
            isinstance(verified, dict) and verified.get("mimeType") == GOOGLE_DOC_MIME_TYPE
        ),
        "trashed": bool(verified.get("trashed")) if isinstance(verified, dict) else False,
    }
    if not verification["passed"]:
        raise RuntimeError("Google Doc trash did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "trash_doc",
        "document_id": document_id,
        "title": metadata.get("name", "") if isinstance(metadata, dict) else "",
        "trashed": bool(metadata.get("trashed")) if isinstance(metadata, dict) else True,
        "verification": verification,
        "url": metadata.get("webViewLink", "") if isinstance(metadata, dict) else "",
        "folder_path": target_folder_path,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_trash(
    document_id_or_url: str,
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Move one exact approved Google Doc in KNIOps to Drive trash."""

    return json.dumps(
        google_doc_trash_impl(
            document_id_or_url,
            folder_path=folder_path,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_list_folder_impl(
    folder_path: str = "",
    *,
    max_items: int = 50,
    live: bool = False,
) -> dict[str, Any]:
    """List files and folders under the scoped KNIOps Drive folder boundary."""

    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_items = min(max(int(max_items or 50), 1), 100)
    if not live:
        return {
            "status": "dry-run",
            "operation": "list_folder",
            "folder_path": target_folder_path,
            "max_items": bounded_items,
            "items": [],
            "item_count": 0,
            "send_enabled": False,
        }
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    if not folder_id:
        return {
            "status": "missing",
            "operation": "list_folder",
            "folder_path": target_folder_path,
            "items": [],
            "item_count": 0,
            "send_enabled": False,
        }
    payload = (
        drive_service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="files(id,name,mimeType,webViewLink,modifiedTime)",
            pageSize=bounded_items,
            orderBy="folder,name",
        )
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return {
        "status": "success",
        "operation": "list_folder",
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "items": [_safe_drive_item(item) for item in files if isinstance(item, dict)],
        "item_count": len([item for item in files if isinstance(item, dict)]),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_list_folder(
    folder_path: str = "",
    max_items: int = 50,
    live: bool = False,
) -> str:
    """List files and subfolders inside the scoped KNIOps Google Drive folder."""

    return json.dumps(
        google_drive_list_folder_impl(
            folder_path,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_search_files_impl(
    query: str = "",
    *,
    folder_path: str = "",
    mime_type: str = "",
    max_items: int = 25,
    live: bool = False,
) -> dict[str, Any]:
    """Search scoped Google Drive files by title text and optional MIME type."""

    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_items = min(max(int(max_items or 25), 1), 100)
    clean_query = " ".join(str(query or "").split())
    clean_mime_type = str(mime_type or "").strip()
    if not live:
        return {
            "status": "dry-run",
            "operation": "search_files",
            "query": clean_query,
            "mime_type": clean_mime_type,
            "folder_path": target_folder_path,
            "max_items": bounded_items,
            "items": [],
            "item_count": 0,
            "send_enabled": False,
            "notes": [
                "Use mime_type='image/' to discover image files by MIME prefix.",
                "This tool returns Drive metadata only; it does not download file bytes.",
            ],
        }
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    if not folder_id:
        return {
            "status": "missing",
            "operation": "search_files",
            "folder_path": target_folder_path,
            "items": [],
            "item_count": 0,
            "send_enabled": False,
        }
    filters = [f"'{folder_id}' in parents", "trashed = false"]
    if clean_query:
        escaped_query = clean_query.replace("'", "\\'")
        filters.append(f"name contains '{escaped_query}'")
    if clean_mime_type:
        if clean_mime_type.endswith("/"):
            filters.append(f"mimeType contains '{clean_mime_type}'")
        else:
            filters.append(f"mimeType = '{clean_mime_type}'")
    payload = (
        drive_service.files()
        .list(
            q=" and ".join(filters),
            spaces="drive",
            fields="files(id,name,mimeType,webViewLink,modifiedTime,size,imageMediaMetadata)",
            pageSize=bounded_items,
            orderBy="modifiedTime desc,name",
        )
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return {
        "status": "success",
        "operation": "search_files",
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "query": clean_query,
        "mime_type": clean_mime_type,
        "items": [_safe_drive_item(item) for item in files if isinstance(item, dict)],
        "item_count": len([item for item in files if isinstance(item, dict)]),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_search_files(
    query: str = "",
    folder_path: str = "",
    mime_type: str = "",
    max_items: int = 25,
    live: bool = False,
) -> str:
    """Search scoped Drive file metadata, including Docs, Sheets, PDFs, and images."""

    return json.dumps(
        google_drive_search_files_impl(
            query,
            folder_path=folder_path,
            mime_type=mime_type,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_get_file_metadata_impl(
    file_id_or_url: str,
    *,
    folder_path: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Read scoped Drive metadata for a specific file, including image metadata."""

    file_id = _google_drive_file_id(file_id_or_url)
    target_folder_path = _google_docs_folder_path(folder_path)
    if not file_id:
        raise ValueError("file_id_or_url is required.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "get_file_metadata",
            "file_id": file_id,
            "folder_path": target_folder_path,
            "send_enabled": False,
            "metadata_fields": [
                "id",
                "name",
                "mimeType",
                "webViewLink",
                "modifiedTime",
                "createdTime",
                "size",
                "imageMediaMetadata",
                "description",
            ],
            "notes": [
                "Returns Drive metadata only; it does not download file bytes.",
                "Image support includes width, height, and rotation metadata when Drive provides it.",
            ],
        }
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    if not folder_id:
        return {
            "status": "missing",
            "operation": "get_file_metadata",
            "file_id": file_id,
            "folder_path": target_folder_path,
            "reason": "Allowed folder path was not found.",
            "send_enabled": False,
        }
    metadata = (
        drive_service.files()
        .get(
            fileId=file_id,
            fields=(
                "id,name,mimeType,webViewLink,modifiedTime,createdTime,size,"
                "imageMediaMetadata,description,trashed,parents"
            ),
            supportsAllDrives=False,
        )
        .execute()
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("Google Drive returned non-object file metadata.")
    _assert_drive_file_under_folder(drive_service, file_id, folder_id)
    return {
        "status": "success",
        "operation": "get_file_metadata",
        "file_id": file_id,
        "folder_path": target_folder_path,
        "file": _safe_drive_item(metadata),
        "created_time": str(metadata.get("createdTime", "")),
        "description": str(metadata.get("description", ""))[:1200],
        "trashed": bool(metadata.get("trashed")),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_get_file_metadata(
    file_id_or_url: str,
    folder_path: str = "",
    live: bool = False,
) -> str:
    """Read scoped Drive file metadata for Docs, Sheets, PDFs, images, and other files."""

    return json.dumps(
        google_drive_get_file_metadata_impl(
            file_id_or_url,
            folder_path=folder_path,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_create_folder_impl(
    folder_path: str = "",
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create KNIOps or a subfolder below it behind explicit approval gates."""

    target_folder_path = _google_docs_folder_path(folder_path)
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_table",
            "folder_path": target_folder_path,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _ensure_drive_folder_path(drive_service, target_folder_path)
    return {
        "status": "success",
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "url": f"https://drive.google.com/drive/folders/{folder_id}",
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_create_folder(
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create an approved folder under the scoped KNIOps Google Drive boundary."""

    return json.dumps(
        google_drive_create_folder_impl(
            folder_path,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_rename_folder_impl(
    folder_path_or_id: str,
    new_name: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Rename a subfolder under KNIOps behind explicit approval gates."""

    target = str(folder_path_or_id or "").strip()
    cleaned_name = _clean_drive_folder_name(new_name)
    if not target:
        raise ValueError("folder_path_or_id is required.")
    scoped_target = _google_docs_folder_path(target)
    if not live:
        return {
            "status": "dry-run",
            "folder_path_or_id": scoped_target,
            "new_name": cleaned_name,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _resolve_drive_folder_id(drive_service, target)
    base_folder_path = _google_docs_folder_path("")
    base_folder_id = _ensure_drive_folder_path(drive_service, base_folder_path)
    if folder_id == base_folder_id:
        raise RuntimeError("Renaming the KNIOps root folder is not allowed from this tool.")
    _assert_drive_file_under_folder(drive_service, folder_id, base_folder_id)
    metadata = (
        drive_service.files()
        .update(fileId=folder_id, body={"name": cleaned_name}, fields="id,name,webViewLink")
        .execute()
    )
    return {
        "status": "success",
        "folder_id": folder_id,
        "name": metadata.get("name", cleaned_name) if isinstance(metadata, dict) else cleaned_name,
        "url": metadata.get("webViewLink", "") if isinstance(metadata, dict) else "",
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_rename_folder(
    folder_path_or_id: str,
    new_name: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Rename an approved subfolder under the scoped KNIOps Google Drive boundary."""

    return json.dumps(
        google_drive_rename_folder_impl(
            folder_path_or_id,
            new_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_remove_folder_impl(
    folder_path_or_id: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Move an empty subfolder under KNIOps to trash behind explicit approval gates."""

    target = str(folder_path_or_id or "").strip()
    if not target:
        raise ValueError("folder_path_or_id is required.")
    scoped_target = _google_docs_folder_path(target)
    if not live:
        return {
            "status": "dry-run",
            "folder_path_or_id": scoped_target,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _resolve_drive_folder_id(drive_service, target)
    base_folder_path = _google_docs_folder_path("")
    base_folder_id = _ensure_drive_folder_path(drive_service, base_folder_path)
    if folder_id == base_folder_id:
        raise RuntimeError("Removing the KNIOps root folder is not allowed from this tool.")
    _assert_drive_file_under_folder(drive_service, folder_id, base_folder_id)
    children = _list_drive_folder_children(drive_service, folder_id, page_size=1)
    if children:
        raise RuntimeError("Refusing to remove a non-empty Google Drive folder.")
    metadata = (
        drive_service.files()
        .update(fileId=folder_id, body={"trashed": True}, fields="id,name,trashed,webViewLink")
        .execute()
    )
    return {
        "status": "success",
        "folder_id": folder_id,
        "name": metadata.get("name", "") if isinstance(metadata, dict) else "",
        "trashed": bool(metadata.get("trashed")) if isinstance(metadata, dict) else True,
        "url": metadata.get("webViewLink", "") if isinstance(metadata, dict) else "",
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_remove_folder(
    folder_path_or_id: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Remove an approved empty subfolder under the scoped KNIOps Google Drive boundary."""

    return json.dumps(
        google_drive_remove_folder_impl(
            folder_path_or_id,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_list_impl(
    folder_path: str = "",
    *,
    max_items: int = 50,
    live: bool = False,
) -> dict[str, Any]:
    """List Google Sheets under a scoped KNIOps Drive folder path."""

    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_items = min(max(int(max_items or 50), 1), 100)
    if not live:
        return {
            "status": "dry-run",
            "folder_path": target_folder_path,
            "max_items": bounded_items,
            "items": [],
            "send_enabled": False,
        }
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    if not folder_id:
        return {
            "status": "missing",
            "folder_path": target_folder_path,
            "items": [],
            "send_enabled": False,
        }
    payload = (
        drive_service.files()
        .list(
            q=(
                f"'{folder_id}' in parents and trashed = false and "
                f"mimeType = '{GOOGLE_SHEET_MIME_TYPE}'"
            ),
            spaces="drive",
            fields="files(id,name,mimeType,webViewLink,modifiedTime)",
            pageSize=bounded_items,
            orderBy="name",
        )
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return {
        "status": "success",
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "items": [_safe_drive_item(item) for item in files if isinstance(item, dict)],
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_list(
    folder_path: str = "",
    max_items: int = 50,
    live: bool = False,
) -> str:
    """List spreadsheet files inside the scoped KNIOps Google Drive boundary."""

    return json.dumps(
        google_sheet_list_impl(
            folder_path,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_create_impl(
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    *,
    folder_path: str = "",
    tabs_json: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create a Google Sheet under KNIOps with optional named tabs."""

    cleaned_title = _clean_spreadsheet_title(title)
    target_folder_path = _google_docs_folder_path(folder_path)
    tabs = _sheet_tabs_from_json(tabs_json)
    if not tabs and cleaned_title == DEFAULT_GOOGLE_SHEETS_WORKBOOK:
        tabs = DEFAULT_GOOGLE_SHEET_TABS
    if not live:
        return {
            "status": "dry-run",
            "operation": "create_sheet",
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "tabs": tabs,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    sheets_service = services["sheets"]
    _assert_configured_google_account(drive_service)
    spreadsheet_body: dict[str, Any] = {"properties": {"title": cleaned_title}}
    if tabs:
        spreadsheet_body["sheets"] = [{"properties": {"title": tab_name}} for tab_name in tabs]
    created = sheets_service.spreadsheets().create(body=spreadsheet_body).execute()
    spreadsheet_id = str(created.get("spreadsheetId", ""))
    if not spreadsheet_id:
        raise RuntimeError("Google Sheets create returned no spreadsheet ID.")
    folder_id = _ensure_drive_folder_path(drive_service, target_folder_path)
    if folder_id:
        _move_drive_file_to_folder(drive_service, spreadsheet_id, folder_id)
    verification = _verify_google_sheet_file(
        drive_service,
        spreadsheet_id,
        expected_title=cleaned_title,
        expected_trashed=False,
    )
    if not verification["passed"]:
        raise RuntimeError("Google Sheet create did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "create_sheet",
        "spreadsheet_id": spreadsheet_id,
        "title": cleaned_title,
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        "folder_path": target_folder_path,
        "tabs": tabs,
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_create(
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    tabs_json: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create an approved spreadsheet under the scoped KNIOps Drive boundary."""

    return json.dumps(
        google_sheet_create_impl(
            title,
            folder_path=folder_path,
            tabs_json=tabs_json,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_read_table_impl(
    spreadsheet_id_or_url: str = "",
    *,
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    range_a1: str = "",
    max_rows: int = 100,
    live: bool = False,
) -> dict[str, Any]:
    """Read rows from a scoped Google Sheet table."""

    cleaned_title = _clean_spreadsheet_title(title)
    target_folder_path = _google_docs_folder_path(folder_path)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    bounded_rows = min(max(int(max_rows or 100), 1), 1000)
    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    read_range = range_a1.strip() or f"{_sheet_name_a1(cleaned_sheet_name)}!1:{bounded_rows}"
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_table",
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": cleaned_sheet_name,
            "range": read_range,
            "rows": [],
            "row_count": 0,
            "send_enabled": False,
        }
    services = _google_workspace_services()
    drive_service = services["drive"]
    sheets_service = services["sheets"]
    _assert_configured_google_account(drive_service)
    target_id = spreadsheet_id or _find_google_sheet_by_title(
        drive_service, cleaned_title, target_folder_path
    )
    if not target_id:
        return {
            "status": "missing",
            "operation": "read_table",
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "rows": [],
            "row_count": 0,
            "send_enabled": False,
        }
    _assert_google_sheet_under_kniops(drive_service, target_id)
    payload = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=target_id, range=read_range, majorDimension="ROWS")
        .execute()
    )
    rows = payload.get("values", []) if isinstance(payload, dict) else []
    return {
        "status": "success",
        "operation": "read_table",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "folder_path": target_folder_path,
        "sheet_name": cleaned_sheet_name,
        "range": read_range,
        "rows": rows[:bounded_rows],
        "row_count": len(rows[:bounded_rows]),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_read_table(
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    range_a1: str = "",
    max_rows: int = 100,
    live: bool = False,
) -> str:
    """Read a table or A1 range from an approved KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_read_table_impl(
            spreadsheet_id_or_url,
            title=title,
            folder_path=folder_path,
            sheet_name=sheet_name,
            range_a1=range_a1,
            max_rows=max_rows,
            live=live or _google_workspace_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_append_rows_impl(
    rows_json: str,
    *,
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Append structured rows to a scoped Google Sheet with header alignment."""

    rows = _json_rows(rows_json)
    cleaned_title = _clean_spreadsheet_title(title)
    target_folder_path = _google_docs_folder_path(folder_path)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    if not live:
        return {
            "status": "dry-run",
            "operation": "append_rows",
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": cleaned_sheet_name,
            "row_count": len(rows),
            "headers": _ordered_row_headers(rows),
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    sheets_service = services["sheets"]
    _assert_configured_google_account(drive_service)
    target_id = spreadsheet_id or _ensure_google_sheet_file(
        services, cleaned_title, target_folder_path, [cleaned_sheet_name]
    )
    _assert_google_sheet_under_kniops(drive_service, target_id)
    _ensure_google_sheet_tab(sheets_service, target_id, cleaned_sheet_name)
    headers = _ensure_google_sheet_headers(sheets_service, target_id, cleaned_sheet_name, rows)
    values = [[_sheet_cell_value(row.get(header, "")) for header in headers] for row in rows]
    response = (
        sheets_service.spreadsheets()
        .values()
        .append(
            spreadsheetId=target_id,
            range=f"{_sheet_name_a1(cleaned_sheet_name)}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )
    updated_range = (
        str(response.get("updates", {}).get("updatedRange", ""))
        if isinstance(response, dict)
        else ""
    )
    verification = _verify_google_sheet_range(
        sheets_service,
        target_id,
        updated_range,
        expected_values=values,
    )
    if not verification["passed"]:
        raise RuntimeError("Google Sheet append did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "append_rows",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "row_count": len(rows),
        "updated_range": updated_range,
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_append_rows(
    rows_json: str,
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Append approved structured rows to a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_append_rows_impl(
            rows_json,
            spreadsheet_id_or_url=spreadsheet_id_or_url,
            title=title,
            folder_path=folder_path,
            sheet_name=sheet_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_update_row_impl(
    fields_json: str,
    *,
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    key_column: str = "record_key",
    key_value: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Update exactly one row in a scoped Google Sheet by stable key."""

    fields = _json_object(fields_json, "fields_json")
    if not key_column.strip() or not key_value.strip():
        raise ValueError("key_column and key_value are required.")
    cleaned_title = _clean_spreadsheet_title(title)
    target_folder_path = _google_docs_folder_path(folder_path)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    if not live:
        return {
            "status": "dry-run",
            "operation": "update_row",
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": cleaned_sheet_name,
            "key_column": key_column.strip(),
            "key_value": key_value.strip(),
            "fields": fields,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    sheets_service = services["sheets"]
    _assert_configured_google_account(drive_service)
    target_id = spreadsheet_id or _find_google_sheet_by_title(
        drive_service, cleaned_title, target_folder_path
    )
    if not target_id:
        raise RuntimeError("Target Google Sheet was not found.")
    _assert_google_sheet_under_kniops(drive_service, target_id)
    table = _read_sheet_values(sheets_service, target_id, cleaned_sheet_name)
    headers, matched_row_number, existing_row = _match_sheet_row(
        table, key_column.strip(), key_value.strip()
    )
    headers = _ensure_header_names(sheets_service, target_id, cleaned_sheet_name, headers, fields)
    by_header = {
        header: existing_row[index] if index < len(existing_row) else ""
        for index, header in enumerate(headers)
    }
    by_header.update({key_column.strip(): key_value.strip(), **fields})
    values = [[_sheet_cell_value(by_header.get(header, "")) for header in headers]]
    update_range = f"{_sheet_name_a1(cleaned_sheet_name)}!A{matched_row_number}"
    sheets_service.spreadsheets().values().update(
        spreadsheetId=target_id,
        range=update_range,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    verification = _verify_google_sheet_keyed_row(
        sheets_service,
        target_id,
        cleaned_sheet_name,
        key_column=key_column.strip(),
        key_value=key_value.strip(),
        expected_fields=fields,
    )
    if not verification["passed"]:
        raise RuntimeError("Google Sheet update did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "update_row",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "row_number": matched_row_number,
        "key_column": key_column.strip(),
        "key_value": key_value.strip(),
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_update_row(
    fields_json: str,
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    key_column: str = "record_key",
    key_value: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Update exactly one approved structured row in a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_update_row_impl(
            fields_json,
            spreadsheet_id_or_url=spreadsheet_id_or_url,
            title=title,
            folder_path=folder_path,
            sheet_name=sheet_name,
            key_column=key_column,
            key_value=key_value,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_delete_rows_impl(
    spreadsheet_id_or_url: str = "",
    *,
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    row_index: int = 0,
    key_column: str = "",
    key_value: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one data row from a scoped Google Sheet by index or stable key."""

    cleaned_title = _clean_spreadsheet_title(title)
    target_folder_path = _google_docs_folder_path(folder_path)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    if row_index and row_index < 2:
        raise ValueError("row_index must refer to a data row, not the header row.")
    if not row_index and not (key_column.strip() and key_value.strip()):
        raise ValueError("Provide row_index or key_column plus key_value.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "delete_rows",
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": cleaned_sheet_name,
            "row_index": row_index,
            "key_column": key_column.strip(),
            "key_value": key_value.strip(),
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    sheets_service = services["sheets"]
    _assert_configured_google_account(drive_service)
    target_id = spreadsheet_id or _find_google_sheet_by_title(
        drive_service, cleaned_title, target_folder_path
    )
    if not target_id:
        raise RuntimeError("Target Google Sheet was not found.")
    _assert_google_sheet_under_kniops(drive_service, target_id)
    table_before = _read_sheet_values(sheets_service, target_id, cleaned_sheet_name)
    target_row_index = row_index
    if not target_row_index:
        _, target_row_index, _ = _match_sheet_row(
            table_before, key_column.strip(), key_value.strip()
        )
    sheet_id = _google_sheet_tab_id(sheets_service, target_id, cleaned_sheet_name)
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=target_id,
        body={
            "requests": [
                {
                    "deleteDimension": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": target_row_index - 1,
                            "endIndex": target_row_index,
                        }
                    }
                }
            ]
        },
    ).execute()
    table_after = _read_sheet_values(sheets_service, target_id, cleaned_sheet_name)
    verification = _verify_google_sheet_deleted_row(
        table_before,
        table_after,
        deleted_row_index=target_row_index,
        key_column=key_column.strip(),
        key_value=key_value.strip(),
    )
    if not verification["passed"]:
        raise RuntimeError("Google Sheet row deletion did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "delete_rows",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "deleted_row_index": target_row_index,
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_delete_rows(
    spreadsheet_id_or_url: str = "",
    title: str = DEFAULT_GOOGLE_SHEETS_WORKBOOK,
    folder_path: str = "",
    sheet_name: str = "Contacts",
    row_index: int = 0,
    key_column: str = "",
    key_value: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved row from a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_delete_rows_impl(
            spreadsheet_id_or_url,
            title=title,
            folder_path=folder_path,
            sheet_name=sheet_name,
            row_index=row_index,
            key_column=key_column,
            key_value=key_value,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_create_tab_impl(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create one worksheet tab inside a scoped KNIOps Google Sheet."""

    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    if not live:
        return {
            "status": "dry-run",
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": cleaned_sheet_name,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    _assert_google_sheet_under_kniops(services["drive"], spreadsheet_id)
    sheet_id = _ensure_google_sheet_tab(services["sheets"], spreadsheet_id, cleaned_sheet_name)
    return {
        "status": "success",
        "spreadsheet_id": spreadsheet_id,
        "sheet_name": cleaned_sheet_name,
        "sheet_id": sheet_id,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_create_tab(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Create an approved worksheet tab in a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_create_tab_impl(
            spreadsheet_id_or_url,
            sheet_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_update_tab_impl(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    new_name: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Rename one worksheet tab inside a scoped KNIOps Google Sheet."""

    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    cleaned_new_name = _clean_sheet_tab_name(new_name)
    if not live:
        return {
            "status": "dry-run",
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": cleaned_sheet_name,
            "new_name": cleaned_new_name,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    _assert_google_sheet_under_kniops(services["drive"], spreadsheet_id)
    sheet_id = _google_sheet_tab_id(services["sheets"], spreadsheet_id, cleaned_sheet_name)
    services["sheets"].spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": sheet_id, "title": cleaned_new_name},
                        "fields": "title",
                    }
                }
            ]
        },
    ).execute()
    return {
        "status": "success",
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": sheet_id,
        "sheet_name": cleaned_new_name,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_update_tab(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    new_name: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Rename an approved worksheet tab in a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_update_tab_impl(
            spreadsheet_id_or_url,
            sheet_name,
            new_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_remove_tab_impl(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Remove one worksheet tab from a scoped KNIOps Google Sheet."""

    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    cleaned_sheet_name = _clean_sheet_tab_name(sheet_name)
    if not live:
        return {
            "status": "dry-run",
            "spreadsheet_id": spreadsheet_id,
            "sheet_name": cleaned_sheet_name,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    _assert_google_sheet_under_kniops(services["drive"], spreadsheet_id)
    sheet_id = _google_sheet_tab_id(services["sheets"], spreadsheet_id, cleaned_sheet_name)
    services["sheets"].spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"deleteSheet": {"sheetId": sheet_id}}]},
    ).execute()
    return {
        "status": "success",
        "spreadsheet_id": spreadsheet_id,
        "removed_sheet_name": cleaned_sheet_name,
        "removed_sheet_id": sheet_id,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_remove_tab(
    spreadsheet_id_or_url: str,
    sheet_name: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Remove an approved worksheet tab from a KNIOps Google Sheet."""

    return json.dumps(
        google_sheet_remove_tab_impl(
            spreadsheet_id_or_url,
            sheet_name,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_sheet_trash_impl(
    spreadsheet_id_or_url: str,
    *,
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Move a scoped KNIOps Google Sheet file to Drive trash, never permanent delete."""

    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    if not live:
        return {
            "status": "dry-run",
            "operation": "trash_sheet",
            "spreadsheet_id": spreadsheet_id,
            "approval_reference": approval_reference.strip(),
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    _assert_google_sheet_under_kniops(drive_service, spreadsheet_id)
    metadata = (
        drive_service.files()
        .update(
            fileId=spreadsheet_id,
            body={"trashed": True},
            fields="id,name,trashed,webViewLink",
        )
        .execute()
    )
    verification = _verify_google_sheet_file(
        drive_service,
        spreadsheet_id,
        expected_title=str(metadata.get("name", "")) if isinstance(metadata, dict) else "",
        expected_trashed=True,
    )
    if not verification["passed"]:
        raise RuntimeError("Google Sheet trash did not pass provider read-back verification.")
    return {
        "status": "success",
        "operation": "trash_sheet",
        "spreadsheet_id": spreadsheet_id,
        "name": metadata.get("name", "") if isinstance(metadata, dict) else "",
        "trashed": bool(metadata.get("trashed")) if isinstance(metadata, dict) else True,
        "verification": verification,
        "url": metadata.get("webViewLink", "") if isinstance(metadata, dict) else "",
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_trash(
    spreadsheet_id_or_url: str,
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Move an approved KNIOps Google Sheet to Drive trash."""

    return json.dumps(
        google_sheet_trash_impl(
            spreadsheet_id_or_url,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _normalize_http_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("A valid http(s) URL is required.")
    return value


def _json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object.")
    return parsed


def _json_rows(value: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("rows_json must be a JSON array of objects.") from exc
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("rows_json must be a non-empty JSON array of objects.")
    rows: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("rows_json must contain only JSON objects.")
        rows.append(item)
    return rows


def _json_string_array(value: str, field_name: str) -> list[str]:
    if not str(value or "").strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be a JSON array of strings.") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{field_name} must be a JSON array of strings.")
    return [str(item).strip() for item in parsed if str(item).strip()]


def _airtable_alias_prefix(base_alias: str) -> str:
    normalized = str(base_alias or "").strip().lower().replace("-", "_").replace(" ", "_")
    return AIRTABLE_BASE_ALIAS_PREFIXES.get(normalized, "")


def _normalize_airtable_lookup_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _infer_airtable_base_alias(
    *,
    base_alias: str = "",
    base_id: str = "",
    base_name: str = "",
    table: str = "",
) -> str:
    """Infer a known base alias from explicit user-facing base or table names."""

    clean_alias = str(base_alias or "").strip()
    if clean_alias or str(base_id or "").strip():
        return clean_alias
    normalized_base = _normalize_airtable_lookup_text(base_name)
    finance_base_names = {
        _normalize_airtable_lookup_text(FINANCE_TAX_TRACKER_BASE_NAME),
        "finance_tax_tracker",
        "tax_tracker",
    }
    if normalized_base in finance_base_names or (
        "finance" in normalized_base and "tax" in normalized_base and "tracker" in normalized_base
    ):
        return "finance_tax_tracker"
    if str(table or "").strip() in FINANCE_TAX_TRACKER_TABLES:
        return "finance_tax_tracker"
    return ""


def _airtable_csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in context_env_value(name).split(",") if item.strip())


def _airtable_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(context_env_value(name, str(default))))
    except ValueError:
        return default


def _airtable_allowed_tables(
    *,
    default: tuple[str, ...] = (),
    prefix: str = "",
) -> tuple[str, ...]:
    configured = _airtable_csv_env(f"{prefix}_ALLOWED_TABLES") if prefix else ()
    if not configured:
        configured = _airtable_csv_env("AIRTABLE_ALLOWED_TABLES")
    return configured or default


def _airtable_base_config(
    *,
    base_alias: str = "",
    base_id: str = "",
    base_name: str = "",
    default_allowed_tables: tuple[str, ...] = (),
) -> dict[str, Any]:
    inferred_alias = _infer_airtable_base_alias(
        base_alias=base_alias,
        base_id=base_id,
        base_name=base_name,
    )
    prefix = _airtable_alias_prefix(inferred_alias)
    resolved_base_id = (
        base_id.strip()
        or (context_env_value(f"{prefix}_BASE_ID").strip() if prefix else "")
        or context_env_value("AIRTABLE_BASE_ID").strip()
    )
    resolved_base_name = (
        base_name.strip()
        or (context_env_value(f"{prefix}_BASE_NAME").strip() if prefix else "")
        or context_env_value("AIRTABLE_BASE_NAME").strip()
    )
    access_token = (
        context_env_value(f"{prefix}_ACCESS_TOKEN").strip() if prefix else ""
    ) or context_env_value("AIRTABLE_ACCESS_TOKEN").strip()
    default_table = (
        context_env_value(f"{prefix}_DEFAULT_TABLE").strip() if prefix else ""
    ) or context_env_value("AIRTABLE_DEFAULT_TABLE").strip()
    default_view = (
        context_env_value(f"{prefix}_DEFAULT_VIEW").strip() if prefix else ""
    ) or context_env_value("AIRTABLE_DEFAULT_VIEW").strip()
    return {
        "base_alias": inferred_alias,
        "env_prefix": prefix,
        "base_id": resolved_base_id,
        "base_name": resolved_base_name,
        "access_token": access_token,
        "default_table": default_table,
        "default_view": default_view,
        "allowed_tables": _airtable_allowed_tables(
            default=default_allowed_tables,
            prefix=prefix,
        ),
    }


def _airtable_table(table: str, *, config: dict[str, Any] | None = None) -> str:
    resolved_config = config or _airtable_base_config()
    table_name = table.strip() or str(resolved_config.get("default_table") or "").strip()
    if not table_name:
        raise RuntimeError("Missing Airtable table. Set AIRTABLE_DEFAULT_TABLE or pass table.")
    allowed = list(resolved_config.get("allowed_tables") or ())
    if allowed and table_name not in allowed:
        raise RuntimeError(f"Airtable table '{table_name}' is not in AIRTABLE_ALLOWED_TABLES.")
    return table_name


def _airtable_schema_fields_for_table(
    schema_result: Mapping[str, Any],
    table_name: str,
) -> list[Mapping[str, Any]]:
    tables = schema_result.get("schema", {}).get("tables", [])
    if not isinstance(tables, list):
        return []
    for table in tables:
        if not isinstance(table, Mapping) or table.get("name") != table_name:
            continue
        fields = table.get("fields", [])
        if not isinstance(fields, list):
            return []
        return [field for field in fields if isinstance(field, Mapping)]
    return []


def _apply_schema_select_override(
    fields: dict[str, Any],
    fields_by_name: Mapping[str, Mapping[str, Any]],
    field_name: str,
    requested_value: str,
    notes: list[str],
) -> None:
    value = str(requested_value or "").strip()
    if not value:
        return
    field = fields_by_name.get(field_name)
    if not field:
        notes.append(f"`{field_name}` was requested but is not present in schema.")
        return
    selected = _schema_select_choice(field, value)
    if not selected:
        notes.append(f"`{field_name}` value `{value}` is not a configured Airtable option.")
        return
    field_type = str(field.get("field_type") or "")
    fields[field_name] = [selected] if field_type == "multipleSelects" else selected


def _apply_model_schema_field_values(
    fields: dict[str, Any],
    schema_fields: list[Mapping[str, Any]],
    field_values_json: str,
    notes: list[str],
) -> None:
    raw = str(field_values_json or "").strip()
    if not raw:
        return
    requested_fields = _json_object(raw, "field_values_json")
    fields_by_name = {
        str(field.get("name") or ""): field
        for field in schema_fields
        if str(field.get("name") or "")
    }
    for field_name, value in requested_fields.items():
        clean_name = str(field_name or "").strip()
        if not clean_name:
            continue
        field = fields_by_name.get(clean_name)
        if not field:
            notes.append(f"`{clean_name}` was model-proposed but is not present in schema.")
            continue
        if bool(field.get("is_computed")):
            notes.append(f"`{clean_name}` is computed and was not written.")
            continue
        coerced = _coerce_airtable_schema_value(field, value)
        if coerced is _UNSET_AIRTABLE_VALUE:
            notes.append(
                f"`{clean_name}` value `{value}` is not compatible with "
                f"{field.get('field_type') or 'unknown'}."
            )
            continue
        fields[clean_name] = coerced


_UNSET_AIRTABLE_VALUE = object()


def _coerce_airtable_write_fields(
    schema_fields: list[Mapping[str, Any]],
    requested_fields: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate and coerce general record fields against one live Airtable schema."""

    fields_by_name = {
        str(field.get("name") or ""): field
        for field in schema_fields
        if str(field.get("name") or "")
    }
    coerced: dict[str, Any] = {}
    errors: list[str] = []
    for raw_name, value in requested_fields.items():
        field_name = str(raw_name or "").strip()
        field = fields_by_name.get(field_name)
        if field is None:
            errors.append(f"Unknown Airtable field `{field_name}`.")
            continue
        field_type = str(field.get("field_type") or "")
        if bool(field.get("is_computed")) or field_type in {"formula", "rollup", "lookup"}:
            errors.append(f"Computed Airtable field `{field_name}` is read-only.")
            continue
        if field_type == "multipleAttachments":
            errors.append(
                f"Attachment field `{field_name}` requires the dedicated attachment tool."
            )
            continue
        field_value = _coerce_airtable_schema_value(field, value)
        if field_value is _UNSET_AIRTABLE_VALUE:
            errors.append(f"Value for `{field_name}` is incompatible with {field_type}.")
            continue
        coerced[field_name] = field_value
    return coerced, errors


def _airtable_verified_field_value(
    verified_fields: Mapping[str, Any],
    requested_name: str,
) -> tuple[bool, Any]:
    """Resolve one read-back field without losing exact provider-name whitespace."""

    if requested_name in verified_fields:
        return True, verified_fields[requested_name]
    normalized_name = requested_name.strip()
    normalized_matches = [
        value
        for name, value in verified_fields.items()
        if str(name).strip() == normalized_name
    ]
    if len(normalized_matches) == 1:
        return True, normalized_matches[0]
    return False, None


def _coerce_airtable_schema_value(field: Mapping[str, Any], value: object) -> object:
    field_type = str(field.get("field_type") or "")
    if value in (None, ""):
        return _UNSET_AIRTABLE_VALUE
    if field_type in {"currency", "number", "percent"}:
        try:
            return float(Decimal(str(value).replace(",", "")))
        except InvalidOperation:
            return _UNSET_AIRTABLE_VALUE
    if field_type == "checkbox":
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
        return _UNSET_AIRTABLE_VALUE
    if field_type == "date":
        raw = str(value).strip()
        return raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else _UNSET_AIRTABLE_VALUE
    if field_type == "singleSelect":
        selected = _schema_select_choice(field, str(value))
        return selected or _UNSET_AIRTABLE_VALUE
    if field_type == "multipleSelects":
        values = value if isinstance(value, list | tuple | set) else [value]
        selected = [_schema_select_choice(field, str(item)) for item in values]
        selected = [item for item in selected if item]
        return selected or _UNSET_AIRTABLE_VALUE
    if field_type == "multipleAttachments":
        return _UNSET_AIRTABLE_VALUE
    return str(value).strip()


def _schema_select_choice(field: Mapping[str, Any], requested: str) -> str:
    value = str(requested or "").strip()
    choices = field.get("select_choices_exact") or field.get("select_choices", [])
    for choice in choices:
        exact_choice = str(choice)
        clean_choice = exact_choice.strip()
        if clean_choice.lower() == value.lower():
            return exact_choice
    return ""


def _model_receipt_evidence_from_json(
    receipt_fields_json: str,
    *,
    source_path: str,
) -> FinanceReceiptEvidence | None:
    raw = str(receipt_fields_json or "").strip()
    if not raw:
        return None
    fields = _json_object(raw, "receipt_fields_json")
    normalized = {
        "vendor": _first_present(fields, "vendor", "merchant", "seller"),
        "receipt_date": _first_present(fields, "receipt_date", "date", "date_of_expense"),
        "order_number": _first_present(fields, "order_number", "order", "receipt_number"),
        "description": _first_present(fields, "description", "item", "service"),
        "quantity": _first_present(fields, "quantity", "qty"),
        "subtotal": _first_present(fields, "subtotal", "amount"),
        "shipping": _first_present(fields, "shipping", "fees", "shipping_or_fees"),
        "total": _first_present(fields, "total", "total_expenses", "total_paid"),
        "currency": _first_present(fields, "currency"),
        "payment_summary": _first_present(fields, "payment_summary", "payment_method"),
        "estimated_tax_periods": _first_present(
            fields,
            "estimated_tax_periods",
            "estimated_tax_period",
            "tax_period",
        ),
    }
    if normalized["receipt_date"] and not normalized["estimated_tax_periods"]:
        normalized["estimated_tax_periods"] = _finance_tax_period_for_date(
            normalized["receipt_date"]
        )
    return FinanceReceiptEvidence(
        source_path=source_path,
        filename=Path(source_path).name,
        content_read=any(value for value in normalized.values()),
        extraction_method="model_receipt_fields",
        blocker="" if any(value for value in normalized.values()) else "empty model receipt fields",
        **normalized,
    )


def _merge_receipt_evidence(
    extracted: FinanceReceiptEvidence,
    model: FinanceReceiptEvidence | None,
) -> tuple[FinanceReceiptEvidence, list[str]]:
    if model is None or not model.content_read:
        return extracted, ["deterministic_extraction_used"] if extracted.content_read else []
    if not extracted.content_read:
        return model, ["model_receipt_fields_used", f"deterministic_blocker: {extracted.blocker}"]
    notes = ["model_receipt_fields_used", "deterministic_extraction_used_as_validation"]
    conflicts: list[str] = []
    for field_name in ("vendor", "receipt_date", "total"):
        extracted_value = getattr(extracted, field_name)
        model_value = getattr(model, field_name)
        if extracted_value and model_value and not _receipt_values_equivalent(
            field_name,
            extracted_value,
            model_value,
        ):
            conflicts.append(f"conflict:{field_name}:{extracted_value}!={model_value}")
    if conflicts:
        return extracted, [*notes, *conflicts]
    merged_values: dict[str, str] = {}
    for field_name in (
        "vendor",
        "receipt_date",
        "order_number",
        "description",
        "quantity",
        "subtotal",
        "shipping",
        "total",
        "currency",
        "payment_summary",
        "estimated_tax_periods",
    ):
        merged_values[field_name] = getattr(model, field_name) or getattr(extracted, field_name)
    return (
        replace(
            extracted,
            extraction_method="model_receipt_fields+deterministic_validation",
            **merged_values,
        ),
        notes,
    )


def _first_present(fields: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = fields.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _receipt_values_equivalent(field_name: str, left: str, right: str) -> bool:
    if field_name == "total":
        try:
            return Decimal(str(left).replace(",", "")) == Decimal(str(right).replace(",", ""))
        except InvalidOperation:
            return str(left).strip() == str(right).strip()
    return " ".join(str(left).lower().split()) == " ".join(str(right).lower().split())


def _finance_tax_period_for_date(date_value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}", str(date_value or "").strip())
    if not match or match.group(1) != "2026":
        return ""
    month = int(match.group(2))
    if month <= 3:
        return "Q1"
    if month <= 5:
        return "Q2"
    if month <= 8:
        return "Q3"
    return "Q4"


def _airtable_base_schema_url(base_id: str) -> str:
    return f"https://api.airtable.com/v0/meta/bases/{quote(base_id, safe='')}/tables"


def _airtable_table_url(table_name: str, *, base_id: str = "") -> str:
    if not base_id:
        base_id = context_env_value("AIRTABLE_BASE_ID").strip() or "app_dry_run"
    return f"https://api.airtable.com/v0/{base_id}/{quote(table_name, safe='')}"


def _require_airtable_credentials(*, base_id: str = "", access_token: str = "") -> None:
    if not (base_id or context_env_value("AIRTABLE_BASE_ID").strip()):
        raise RuntimeError("Missing Airtable configuration: AIRTABLE_BASE_ID.")
    if not (access_token or context_env_value("AIRTABLE_ACCESS_TOKEN").strip()):
        raise RuntimeError("Missing Airtable configuration: AIRTABLE_ACCESS_TOKEN.")


def _airtable_send(request: dict[str, Any], *, access_token: str = "") -> dict[str, Any]:
    query = urlencode(request.get("params", {}))
    request_url = request["url"] if not query else f"{request['url']}?{query}"
    payload = request.get("payload")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    outbound = Request(request_url, data=body, method=str(request["method"]))
    token = access_token or context_env_value("AIRTABLE_ACCESS_TOKEN")
    outbound.add_header("Authorization", f"Bearer {token}")
    outbound.add_header("Content-Type", "application/json")
    outbound.add_header("Accept", "application/json")
    timeout_seconds = int(context_env_value("AIRTABLE_REQUEST_TIMEOUT_SECONDS", "20"))
    try:
        with urlopen(outbound, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw_detail = exc.read().decode("utf-8", errors="replace")[:2000]
        try:
            parsed_detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed_detail = {}
        error = parsed_detail.get("error") if isinstance(parsed_detail, Mapping) else {}
        if isinstance(error, Mapping):
            error_type = str(error.get("type") or "").strip()
            message = str(error.get("message") or "").strip()
        else:
            error_type = str(error or "").strip()
            message = ""
        safe_detail = ": ".join(part for part in (error_type, message) if part)[:700]
        raise RuntimeError(
            f"Airtable API request failed with HTTP {exc.code}"
            + (f": {safe_detail}" if safe_detail else ".")
        ) from exc


def _safe_request_preview(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": request.get("method"),
        "url": re.sub(
            r"/(app[^/]+)(/|$)",
            r"/<base_id>\2",
            str(request.get("url", "")),
        ),
        "table": request.get("table"),
        "params": request.get("params", {}),
        "payload": request.get("payload", {}),
    }


def _save_airtable_schema_memory(
    summary: AirtableBaseSchemaSummary,
    *,
    database_url: str | None,
) -> int:
    store = SQLiteStore(database_url or database_url_from_env())
    item = chief_of_staff_memory_item(
        memory_type="operator_reference",
        title=f"Airtable schema: {summary.base_name}",
        summary=(
            f"{summary.base_name} has {summary.table_count} visible tables. "
            f"Allowed finance/tax tables: {', '.join(summary.allowed_tables)}."
        ),
        object_id=summary.base_name,
        object_key="finance-tax-tracker",
        content={
            "base_id": summary.base_id,
            "base_name": summary.base_name,
            "allowed_tables": summary.allowed_tables,
            "tables": [
                {
                    "name": table.name,
                    "table_id": table.table_id,
                    "fields": [
                        {
                            "name": field.name,
                            "field_id": field.field_id,
                            "field_type": field.field_type,
                            "is_computed": field.is_computed,
                        }
                        for field in table.fields
                    ],
                }
                for table in summary.tables
            ],
            "missing_allowed_tables": summary.missing_allowed_tables,
        },
        source_ids=[f"airtable_schema:{summary.base_id or 'configured_base'}"],
        confidence=0.9,
    )
    return int(store.save_memory_item(item))


def _write_airtable_schema_context_doc(
    summary: AirtableBaseSchemaSummary,
    context_doc_path: str,
) -> Path:
    root = Path.cwd().resolve()
    raw_path = Path(context_doc_path or DEFAULT_FINANCE_TAX_TRACKER_CONTEXT_DOC)
    target = raw_path if raw_path.is_absolute() else root / raw_path
    target = target.resolve()
    if target != root and root not in target.parents:
        raise ValueError("context_doc_path must stay inside the current workspace.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(airtable_schema_context_markdown(summary), encoding="utf-8")
    return target


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if "token" not in key.lower()}


def _google_doc_id(document_id_or_url: str) -> str:
    value = str(document_id_or_url or "").strip()
    if not value:
        raise ValueError("document_id_or_url is required.")
    match = re.search(r"/document/d/([^/]+)", value)
    return match.group(1) if match else value


def _google_sheet_id(spreadsheet_id_or_url: str) -> str:
    value = str(spreadsheet_id_or_url or "").strip()
    if not value:
        return ""
    match = re.search(r"/spreadsheets/d/([^/]+)", value)
    return match.group(1) if match else value


def _google_drive_file_id(file_id_or_url: str) -> str:
    value = str(file_id_or_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        query_id = parse_qs(parsed.query).get("id", [""])[0]
        if query_id:
            return query_id
        match = re.search(r"/(?:file|document|spreadsheets|presentation)/d/([^/]+)", parsed.path)
        if match:
            return match.group(1)
    return value


def _google_page_element_text(element: dict[str, Any]) -> str:
    chunks: list[str] = []
    shape = element.get("shape")
    if isinstance(shape, dict):
        text = shape.get("text")
        if isinstance(text, dict):
            for entry in text.get("textElements", []):
                if not isinstance(entry, dict):
                    continue
                run = entry.get("textRun")
                if isinstance(run, dict):
                    chunks.append(str(run.get("content") or ""))
    table = element.get("table")
    if isinstance(table, dict):
        for row in table.get("tableRows", []):
            if not isinstance(row, dict):
                continue
            for cell in row.get("tableCells", []):
                if not isinstance(cell, dict):
                    continue
                text = cell.get("text")
                if not isinstance(text, dict):
                    continue
                for entry in text.get("textElements", []):
                    run = entry.get("textRun") if isinstance(entry, dict) else None
                    if isinstance(run, dict):
                        chunks.append(str(run.get("content") or ""))
    group = element.get("elementGroup")
    if isinstance(group, dict):
        for child in group.get("children", []):
            if isinstance(child, dict):
                chunks.append(_google_page_element_text(child))
    return "".join(chunks)


def _clean_slide_text(value: str, max_chars: int) -> tuple[str, bool]:
    normalized = "\n".join(
        line.strip() for line in str(value or "").splitlines() if line.strip()
    )
    return normalized[:max_chars], len(normalized) > max_chars


def _google_slides_artifacts(
    presentation: object,
    *,
    presentation_id: str,
    max_slides: int,
    max_chars: int,
    include_speaker_notes: bool,
) -> dict[str, Any]:
    if not isinstance(presentation, dict):
        raise RuntimeError("Google Slides API returned an unexpected payload.")
    slides: list[dict[str, Any]] = []
    for position, slide in enumerate(presentation.get("slides", []), start=1):
        if not isinstance(slide, dict):
            continue
        page_elements = [
            element for element in slide.get("pageElements", []) if isinstance(element, dict)
        ]
        text_raw = "\n".join(_google_page_element_text(element) for element in page_elements)
        text, text_truncated = _clean_slide_text(text_raw, max_chars)
        title = ""
        for element in page_elements:
            shape = element.get("shape")
            placeholder = shape.get("placeholder") if isinstance(shape, dict) else None
            if isinstance(placeholder, dict) and placeholder.get("type") in {
                "TITLE",
                "CENTERED_TITLE",
            }:
                title, _unused = _clean_slide_text(_google_page_element_text(element), 500)
                break
        if not title:
            title = next((line for line in text.splitlines() if line), "")[:500]
        notes = ""
        notes_truncated = False
        if include_speaker_notes:
            notes_page = slide.get("slideProperties", {}).get("notesPage", {})
            if isinstance(notes_page, dict):
                notes_raw = "\n".join(
                    _google_page_element_text(element)
                    for element in notes_page.get("pageElements", [])
                    if isinstance(element, dict)
                )
                notes, notes_truncated = _clean_slide_text(notes_raw, max_chars)
        object_id = str(slide.get("objectId") or "")
        slides.append(
            {
                "slide_number": position,
                "slide_id": object_id or f"{presentation_id}:slide:{position}",
                "title": title,
                "text": text,
                "speaker_notes": notes,
                "text_truncated": text_truncated,
                "speaker_notes_truncated": notes_truncated,
            }
        )
    return {"all_slides": slides, "requested_max_slides": max_slides}


def _powerpoint_part_number(name: str) -> int:
    match = re.search(r"(\d+)\.xml$", name)
    return int(match.group(1)) if match else 0


def _powerpoint_xml_text(archive: zipfile.ZipFile, name: str) -> str:
    info = archive.getinfo(name)
    if info.file_size > 5_000_000:
        raise RuntimeError("A PowerPoint XML part exceeds the bounded read limit.")
    root = ElementTree.fromstring(archive.read(name))
    return "\n".join(
        str(node.text or "").strip()
        for node in root.findall(".//{*}t")
        if str(node.text or "").strip()
    )


def _powerpoint_slide_artifacts(
    content: bytes,
    *,
    presentation_id: str,
    max_slides: int,
    max_chars: int,
    include_speaker_notes: bool,
) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise RuntimeError("PowerPoint content is not a valid Open XML deck.") from exc
    with archive:
        if sum(info.file_size for info in archive.infolist()) > 100_000_000:
            raise RuntimeError("PowerPoint expanded content exceeds the bounded read limit.")
        slide_names = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=_powerpoint_part_number,
        )
        notes_names = {
            _powerpoint_part_number(name): name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
        }
        slides: list[dict[str, Any]] = []
        for position, name in enumerate(slide_names, start=1):
            text, text_truncated = _clean_slide_text(
                _powerpoint_xml_text(archive, name), max_chars
            )
            notes = ""
            notes_truncated = False
            notes_name = notes_names.get(_powerpoint_part_number(name))
            if include_speaker_notes and notes_name:
                notes, notes_truncated = _clean_slide_text(
                    _powerpoint_xml_text(archive, notes_name), max_chars
                )
            slides.append(
                {
                    "slide_number": position,
                    "slide_id": f"{presentation_id}:snapshot-slide:{position}",
                    "title": next((line for line in text.splitlines() if line), "")[:500],
                    "text": text,
                    "speaker_notes": notes,
                    "text_truncated": text_truncated,
                    "speaker_notes_truncated": notes_truncated,
                }
            )
    return {"all_slides": slides, "requested_max_slides": max_slides}


def _google_workspace_services() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Install google-api-python-client and google-auth to use live Google Workspace tools."
        ) from exc

    token_path = _google_workspace_token_path()
    if not token_path.exists():
        raise RuntimeError(f"Google Workspace OAuth token is missing at {token_path}.")
    credentials = Credentials.from_authorized_user_file(str(token_path), GOOGLE_WORKSPACE_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
    if not credentials.valid:
        raise RuntimeError("Google Workspace OAuth token is not valid.")
    return {
        "docs": build("docs", "v1", credentials=credentials, cache_discovery=False),
        "drive": build("drive", "v3", credentials=credentials, cache_discovery=False),
        "sheets": build("sheets", "v4", credentials=credentials, cache_discovery=False),
        "slides": build("slides", "v1", credentials=credentials, cache_discovery=False),
    }


def _google_workspace_token_path() -> Path:
    if context_env_value("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH").strip():
        return context_env_path("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH")
    if context_env_value("GOOGLE_TOKEN_FILE").strip():
        return context_env_path("GOOGLE_TOKEN_FILE")
    return Path(".local/google-workspace-oauth-token.json")


def _google_docs_service() -> Any:
    return _google_workspace_services()["docs"]


def _google_docs_folder_path(folder_path: str = "") -> str:
    base_path = _google_drive_base_folder_path()
    requested_parts = _drive_path_parts(str(folder_path or ""))
    base_parts = _drive_path_parts(base_path)
    if not requested_parts:
        return " / ".join(base_parts)
    if [part.lower() for part in requested_parts[: len(base_parts)]] == [
        part.lower() for part in base_parts
    ]:
        return " / ".join(requested_parts)
    return " / ".join([*base_parts, *requested_parts])


def _google_drive_base_folder_path() -> str:
    return (
        context_env_value("KEYSTONE_GOOGLE_DOCS_FOLDER").strip()
        or context_env_value("GOOGLE_DRIVE_KNI_OPS_FOLDER").strip()
        or DEFAULT_GOOGLE_DOCS_FOLDER
    )


def _drive_path_parts(folder_path: str) -> list[str]:
    return [part.strip() for part in str(folder_path or "").split("/") if part.strip()]


def _clean_drive_folder_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        raise ValueError("new_name is required.")
    if "/" in cleaned:
        raise ValueError("Folder names must be a single path segment.")
    return cleaned


def _require_google_workspace_write_approval(approval_reference: str) -> None:
    if not approval_reference.strip():
        raise RuntimeError("Google Workspace live writes require a non-empty approval_reference.")
    if not parse_bool(os.getenv("GOOGLE_WORKSPACE_WRITES_ENABLED")):
        raise RuntimeError(
            "Google Workspace writes are disabled. Set GOOGLE_WORKSPACE_WRITES_ENABLED=true."
        )


def _assert_configured_google_account(drive_service: Any) -> None:
    expected = context_env_value("GOOGLE_DRIVE_ACCOUNT").strip().lower()
    require_match = context_env_value("GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH", "true")
    if not expected or not parse_bool(require_match):
        return
    payload = drive_service.about().get(fields="user(emailAddress)").execute()
    user = payload.get("user", {}) if isinstance(payload, dict) else {}
    actual = str(user.get("emailAddress", "")).strip().lower() if isinstance(user, dict) else ""
    if actual and actual != expected:
        raise RuntimeError(
            f"Google Workspace OAuth account is {actual}, but GOOGLE_DRIVE_ACCOUNT is {expected}."
        )


def _assert_drive_file_in_folder(drive_service: Any, file_id: str, folder_path: str) -> None:
    folder_id = _ensure_drive_folder_path(drive_service, folder_path)
    metadata = drive_service.files().get(fileId=file_id, fields="id, name, parents").execute()
    parents = metadata.get("parents", []) if isinstance(metadata, dict) else []
    if folder_id not in parents:
        raise RuntimeError(
            f"Google Doc {file_id} is outside the allowed Google Drive folder '{folder_path}'."
        )


def _assert_google_sheet_under_kniops(drive_service: Any, spreadsheet_id: str) -> None:
    if not spreadsheet_id.strip():
        raise ValueError("spreadsheet_id_or_url is required.")
    metadata = (
        drive_service.files()
        .get(fileId=spreadsheet_id, fields="id,mimeType,parents", supportsAllDrives=False)
        .execute()
    )
    if not isinstance(metadata, dict) or metadata.get("mimeType") != GOOGLE_SHEET_MIME_TYPE:
        raise RuntimeError("Target is not a Google Sheet.")
    base_folder_id = _ensure_drive_folder_path(drive_service, _google_docs_folder_path(""))
    _assert_drive_file_under_folder(drive_service, spreadsheet_id, base_folder_id)


def _assert_drive_file_under_folder(drive_service: Any, file_id: str, root_folder_id: str) -> None:
    pending = [
        str(parent) for parent in _drive_file_parents(drive_service, file_id) if str(parent).strip()
    ]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == root_folder_id:
            return
        pending.extend(
            str(parent)
            for parent in _drive_file_parents(drive_service, current)
            if str(parent).strip()
        )
    raise RuntimeError("Target folder is outside the allowed KNIOps Google Drive boundary.")


def _drive_file_parents(drive_service: Any, file_id: str) -> list[str]:
    metadata = drive_service.files().get(fileId=file_id, fields="id, parents").execute()
    parents = metadata.get("parents", []) if isinstance(metadata, dict) else []
    return [str(parent) for parent in parents]


def _ensure_drive_folder_path(drive_service: Any, folder_path: str) -> str:
    parent_id = "root"
    for part in [item.strip() for item in folder_path.split("/") if item.strip()]:
        folder_id = _find_drive_child_folder(drive_service, parent_id, part)
        if not folder_id:
            body: dict[str, Any] = {"name": part, "mimeType": GOOGLE_FOLDER_MIME_TYPE}
            if parent_id:
                body["parents"] = [parent_id]
            response = drive_service.files().create(body=body, fields="id").execute()
            folder_id = str(response.get("id", ""))
        parent_id = folder_id
    return "" if parent_id == "root" else parent_id


def _find_drive_folder_path(drive_service: Any, folder_path: str) -> str:
    parent_id = "root"
    for part in _drive_path_parts(folder_path):
        folder_id = _find_drive_child_folder(drive_service, parent_id, part)
        if not folder_id:
            return ""
        parent_id = folder_id
    return "" if parent_id == "root" else parent_id


def _resolve_drive_folder_id(drive_service: Any, folder_path_or_id: str) -> str:
    value = str(folder_path_or_id or "").strip()
    folder_id = _find_drive_folder_path(drive_service, _google_docs_folder_path(value))
    if folder_id:
        return folder_id
    metadata = (
        drive_service.files()
        .get(fileId=value, fields="id,mimeType,parents", supportsAllDrives=False)
        .execute()
    )
    if not isinstance(metadata, dict) or metadata.get("mimeType") != GOOGLE_FOLDER_MIME_TYPE:
        raise RuntimeError("Target is not a Google Drive folder.")
    return str(metadata.get("id", ""))


def _find_drive_child_folder(drive_service: Any, parent_id: str, name: str) -> str:
    escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query_parts = [
        f"name = '{escaped_name}'",
        f"mimeType = '{GOOGLE_FOLDER_MIME_TYPE}'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")
    payload = (
        drive_service.files()
        .list(q=" and ".join(query_parts), spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    if not files:
        return ""
    return str(files[0].get("id", ""))


def _find_google_sheet_by_title(drive_service: Any, title: str, folder_path: str) -> str:
    folder_id = _find_drive_folder_path(drive_service, folder_path)
    if not folder_id:
        return ""
    escaped_title = title.replace("\\", "\\\\").replace("'", "\\'")
    payload = (
        drive_service.files()
        .list(
            q=(
                f"name = '{escaped_title}' and mimeType = '{GOOGLE_SHEET_MIME_TYPE}' "
                f"and trashed = false and '{folder_id}' in parents"
            ),
            spaces="drive",
            fields="files(id,name)",
            pageSize=2,
        )
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    if len(files) > 1:
        raise RuntimeError(f"Multiple Google Sheets named '{title}' exist in {folder_path}.")
    if not files:
        return ""
    return str(files[0].get("id", ""))


def _ensure_google_sheet_file(
    services: dict[str, Any],
    title: str,
    folder_path: str,
    tabs: list[str],
) -> str:
    existing = _find_google_sheet_by_title(services["drive"], title, folder_path)
    if existing:
        return existing
    spreadsheet_body: dict[str, Any] = {"properties": {"title": title}}
    cleaned_tabs = [_clean_sheet_tab_name(tab) for tab in tabs if str(tab).strip()]
    if cleaned_tabs:
        spreadsheet_body["sheets"] = [
            {"properties": {"title": tab_name}} for tab_name in cleaned_tabs
        ]
    created = services["sheets"].spreadsheets().create(body=spreadsheet_body).execute()
    spreadsheet_id = str(created.get("spreadsheetId", ""))
    folder_id = _ensure_drive_folder_path(services["drive"], folder_path)
    if folder_id:
        _move_drive_file_to_folder(services["drive"], spreadsheet_id, folder_id)
    return spreadsheet_id


def _list_drive_folder_children(
    drive_service: Any,
    folder_id: str,
    *,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    payload = (
        drive_service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="files(id,name,mimeType)",
            pageSize=page_size,
        )
        .execute()
    )
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return [item for item in files if isinstance(item, dict)]


def _move_drive_file_to_folder(drive_service: Any, file_id: str, folder_id: str) -> None:
    metadata = drive_service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(metadata.get("parents", [])) if isinstance(metadata, dict) else ""
    drive_service.files().update(
        fileId=file_id,
        addParents=folder_id,
        removeParents=previous_parents,
        fields="id, parents",
    ).execute()


def _clean_spreadsheet_title(title: str) -> str:
    cleaned = " ".join(str(title or DEFAULT_GOOGLE_SHEETS_WORKBOOK).split())
    if not cleaned:
        raise ValueError("title is required.")
    return cleaned


def _clean_sheet_tab_name(sheet_name: str) -> str:
    cleaned = " ".join(str(sheet_name or "Sheet1").split())
    if not cleaned:
        raise ValueError("sheet_name is required.")
    if len(cleaned) > 100:
        raise ValueError("sheet_name must be 100 characters or fewer.")
    if any(char in cleaned for char in ("[", "]", "*", "?", "/", "\\")):
        raise ValueError("sheet_name contains characters Google Sheets does not allow.")
    return cleaned


def _sheet_tabs_from_json(tabs_json: str) -> list[str]:
    return [_clean_sheet_tab_name(tab) for tab in _json_string_array(tabs_json, "tabs_json")]


def _sheet_name_a1(sheet_name: str) -> str:
    return "'" + sheet_name.replace("'", "''") + "'"


def _sheet_cell_value(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    if isinstance(value, str | int | float | bool):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _ordered_row_headers(rows: list[dict[str, Any]]) -> list[str]:
    headers: list[str] = []
    for row in rows:
        for key in row:
            header = str(key).strip()
            if header and header not in headers:
                headers.append(header)
    return headers


def _sheet_verification_value(value: Any) -> str:
    """Normalize scalar Sheet values for bounded provider read-back checks."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _sheet_verification_row(row: list[Any]) -> list[str]:
    normalized = [_sheet_verification_value(cell) for cell in row]
    while normalized and normalized[-1] == "":
        normalized.pop()
    return normalized


def _verify_google_sheet_file(
    drive_service: Any,
    spreadsheet_id: str,
    *,
    expected_title: str,
    expected_trashed: bool,
) -> dict[str, Any]:
    metadata = (
        drive_service.files()
        .get(
            fileId=spreadsheet_id,
            fields="id,name,mimeType,trashed",
            supportsAllDrives=False,
        )
        .execute()
    )
    actual_id = str(metadata.get("id", "")) if isinstance(metadata, dict) else ""
    actual_title = str(metadata.get("name", "")) if isinstance(metadata, dict) else ""
    actual_mime_type = str(metadata.get("mimeType", "")) if isinstance(metadata, dict) else ""
    actual_trashed = bool(metadata.get("trashed")) if isinstance(metadata, dict) else False
    spreadsheet_id_match = actual_id == spreadsheet_id
    title_match = not expected_title or actual_title == expected_title
    mime_type_match = actual_mime_type == GOOGLE_SHEET_MIME_TYPE
    trashed_match = actual_trashed is expected_trashed
    return {
        "status": "verified"
        if spreadsheet_id_match and title_match and mime_type_match and trashed_match
        else "verification_failed",
        "passed": spreadsheet_id_match and title_match and mime_type_match and trashed_match,
        "spreadsheet_id_match": spreadsheet_id_match,
        "title_match": title_match,
        "mime_type_match": mime_type_match,
        "trashed": actual_trashed,
        "trashed_match": trashed_match,
    }


def _verify_google_sheet_range(
    sheets_service: Any,
    spreadsheet_id: str,
    range_a1: str,
    *,
    expected_values: list[list[Any]],
) -> dict[str, Any]:
    if not range_a1:
        return {
            "status": "verification_failed",
            "passed": False,
            "row_count_match": False,
            "values_match": False,
        }
    payload = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_a1, majorDimension="ROWS")
        .execute()
    )
    actual_values = payload.get("values", []) if isinstance(payload, dict) else []
    normalized_actual = [
        _sheet_verification_row(row) for row in actual_values if isinstance(row, list)
    ]
    normalized_expected = [_sheet_verification_row(row) for row in expected_values]
    row_count_match = len(normalized_actual) == len(normalized_expected)
    values_match = normalized_actual == normalized_expected
    return {
        "status": "verified" if row_count_match and values_match else "verification_failed",
        "passed": row_count_match and values_match,
        "row_count_match": row_count_match,
        "values_match": values_match,
        "verified_row_count": len(normalized_actual),
    }


def _verify_google_sheet_keyed_row(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    *,
    key_column: str,
    key_value: str,
    expected_fields: dict[str, Any],
) -> dict[str, Any]:
    table = _read_sheet_values(sheets_service, spreadsheet_id, sheet_name)
    try:
        headers, row_number, row = _match_sheet_row(table, key_column, key_value)
    except RuntimeError:
        return {
            "status": "verification_failed",
            "passed": False,
            "row_found": False,
            "matched_fields": [],
            "mismatched_fields": sorted(str(key) for key in expected_fields),
        }
    record = {
        header: row[index] if index < len(row) else "" for index, header in enumerate(headers)
    }
    matched_fields = sorted(
        str(key)
        for key, value in expected_fields.items()
        if _sheet_verification_value(record.get(str(key), ""))
        == _sheet_verification_value(_sheet_cell_value(value))
    )
    mismatched_fields = sorted(str(key) for key in expected_fields if str(key) not in matched_fields)
    passed = not mismatched_fields
    return {
        "status": "verified" if passed else "verification_failed",
        "passed": passed,
        "row_found": True,
        "row_number": row_number,
        "matched_fields": matched_fields,
        "mismatched_fields": mismatched_fields,
    }


def _verify_google_sheet_deleted_row(
    table_before: list[list[str]],
    table_after: list[list[str]],
    *,
    deleted_row_index: int,
    key_column: str,
    key_value: str,
) -> dict[str, Any]:
    row_count_delta = len(table_before) - len(table_after)
    row_absent = True
    if key_column and key_value and table_after:
        headers = [str(item).strip() for item in table_after[0]]
        if key_column not in headers:
            row_absent = False
        else:
            key_index = headers.index(key_column)
            row_absent = all(
                str(row[key_index] if key_index < len(row) else "") != key_value
                for row in table_after[1:]
            )
    passed = row_count_delta == 1 and row_absent
    return {
        "status": "verified" if passed else "verification_failed",
        "passed": passed,
        "deleted_row_index": deleted_row_index,
        "row_count_delta": row_count_delta,
        "row_absent": row_absent,
    }


def _read_sheet_values(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> list[list[str]]:
    payload = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=f"{_sheet_name_a1(sheet_name)}!A:ZZ",
            majorDimension="ROWS",
        )
        .execute()
    )
    values = payload.get("values", []) if isinstance(payload, dict) else []
    return [[str(cell) for cell in row] for row in values if isinstance(row, list)]


def _ensure_google_sheet_headers(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    rows: list[dict[str, Any]],
) -> list[str]:
    table = _read_sheet_values(sheets_service, spreadsheet_id, sheet_name)
    existing_headers = [str(item).strip() for item in table[0]] if table else []
    return _ensure_header_names(
        sheets_service,
        spreadsheet_id,
        sheet_name,
        existing_headers,
        {header: "" for header in _ordered_row_headers(rows)},
    )


def _ensure_header_names(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
    existing_headers: list[str],
    fields: dict[str, Any],
) -> list[str]:
    headers = [header for header in existing_headers if header]
    for header in fields:
        cleaned = str(header).strip()
        if cleaned and cleaned not in headers:
            headers.append(cleaned)
    if not headers:
        raise ValueError("At least one row field/header is required.")
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{_sheet_name_a1(sheet_name)}!A1",
        valueInputOption="RAW",
        body={"values": [headers]},
    ).execute()
    return headers


def _match_sheet_row(
    table: list[list[str]],
    key_column: str,
    key_value: str,
) -> tuple[list[str], int, list[str]]:
    if not table:
        raise RuntimeError("Target sheet has no header row.")
    headers = [str(item).strip() for item in table[0]]
    if key_column not in headers:
        raise RuntimeError(f"Key column '{key_column}' was not found.")
    key_index = headers.index(key_column)
    matches: list[tuple[int, list[str]]] = []
    for row_number, row in enumerate(table[1:], start=2):
        candidate = row[key_index] if key_index < len(row) else ""
        if str(candidate) == key_value:
            matches.append((row_number, row))
    if not matches:
        raise RuntimeError("No matching row found for keyed update.")
    if len(matches) > 1:
        raise RuntimeError("Multiple matching rows found for keyed update.")
    return headers, matches[0][0], matches[0][1]


def _google_sheet_tab_metadata(
    sheets_service: Any,
    spreadsheet_id: str,
) -> dict[str, int]:
    payload = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute()
    )
    sheets = payload.get("sheets", []) if isinstance(payload, dict) else []
    tabs: dict[str, int] = {}
    for sheet in sheets:
        props = sheet.get("properties", {}) if isinstance(sheet, dict) else {}
        title = str(props.get("title", ""))
        sheet_id = props.get("sheetId")
        if title and sheet_id is not None:
            tabs[title] = int(sheet_id)
    return tabs


def _google_sheet_tab_id(sheets_service: Any, spreadsheet_id: str, sheet_name: str) -> int:
    tabs = _google_sheet_tab_metadata(sheets_service, spreadsheet_id)
    if sheet_name not in tabs:
        raise RuntimeError(f"Google Sheet tab '{sheet_name}' was not found.")
    return tabs[sheet_name]


def _ensure_google_sheet_tab(
    sheets_service: Any,
    spreadsheet_id: str,
    sheet_name: str,
) -> int:
    tabs = _google_sheet_tab_metadata(sheets_service, spreadsheet_id)
    if sheet_name in tabs:
        return tabs[sheet_name]
    response = (
        sheets_service.spreadsheets()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
        )
        .execute()
    )
    replies = response.get("replies", []) if isinstance(response, dict) else []
    if replies and isinstance(replies[0], dict):
        props = replies[0].get("addSheet", {}).get("properties", {})
        if isinstance(props, dict) and props.get("sheetId") is not None:
            return int(props["sheetId"])
    return _google_sheet_tab_id(sheets_service, spreadsheet_id, sheet_name)


def _google_doc_text(document: dict[str, Any]) -> str:
    parts: list[str] = []
    body = document.get("body", {}) if isinstance(document, dict) else {}
    for item in body.get("content", []) if isinstance(body, dict) else []:
        paragraph = item.get("paragraph", {}) if isinstance(item, dict) else {}
        for element in paragraph.get("elements", []) if isinstance(paragraph, dict) else []:
            text_run = element.get("textRun", {}) if isinstance(element, dict) else {}
            content = text_run.get("content", "") if isinstance(text_run, dict) else ""
            if content:
                parts.append(str(content))
    return "".join(parts).strip()


def _safe_drive_item(item: dict[str, Any]) -> dict[str, Any]:
    mime_type = str(item.get("mimeType", ""))
    if mime_type == GOOGLE_FOLDER_MIME_TYPE:
        item_type = "folder"
    elif mime_type == "application/vnd.google-apps.document":
        item_type = "google_doc"
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        item_type = "google_sheet"
    else:
        item_type = "file"
    payload: dict[str, Any] = {
        "id": str(item.get("id", "")),
        "name": str(item.get("name", "")),
        "mime_type": mime_type,
        "type": item_type,
        "url": str(item.get("webViewLink", "")),
        "modified_time": str(item.get("modifiedTime", "")),
    }
    if item.get("size") is not None:
        payload["size"] = str(item.get("size", ""))
    if isinstance(item.get("imageMediaMetadata"), dict):
        metadata = item["imageMediaMetadata"]
        payload["image_media_metadata"] = {
            "width": str(metadata.get("width", "")),
            "height": str(metadata.get("height", "")),
            "rotation": str(metadata.get("rotation", "")),
        }
    return payload


def _replace_doc_requests(docs_service: Any, document_id: str, body: str) -> list[dict[str, Any]]:
    document = docs_service.documents().get(documentId=document_id).execute()
    content = document.get("body", {}).get("content", []) if isinstance(document, dict) else []
    end_index = 1
    if content and isinstance(content[-1], dict):
        end_index = int(content[-1].get("endIndex", 1))
    requests: list[dict[str, Any]] = []
    if end_index > 2:
        requests.append(
            {"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}}
        )
    requests.append({"insertText": {"location": {"index": 1}, "text": body}})
    return requests
