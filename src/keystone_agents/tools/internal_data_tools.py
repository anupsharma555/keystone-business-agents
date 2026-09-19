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
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from pydantic import Field

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
from keystone_agents.provider_read import (
    ProviderReadContextError,
    current_provider_read_context,
    record_provider_read_result,
)
from keystone_agents.receipts.journal import durable_provider_tool, record_provider_observation
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.schemas.airtable import (
    AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE,
    AIRTABLE_SCHEMA_MAX_TABLES,
    FINANCE_TAX_TRACKER_BASE_NAME,
    FINANCE_TAX_TRACKER_TABLES,
    AirtableBaseSchemaSummary,
    airtable_base_schema_summary_from_metadata,
    airtable_field_summary_from_metadata,
    airtable_schema_context_markdown,
    airtable_schema_preview_payload,
    airtable_schema_snapshot_sha256,
)
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.website_extraction_tool import (
    WebsiteExtractionError,
    extract_website_content,
    extract_website_content_with_fallbacks,
)

GOOGLE_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations",
]
GOOGLE_WORKSPACE_SERVICE_SPECS: dict[str, tuple[str, str, str]] = {
    "drive": ("drive", "v3", "https://www.googleapis.com/auth/drive"),
    "docs": ("docs", "v1", "https://www.googleapis.com/auth/documents"),
    "sheets": ("sheets", "v4", "https://www.googleapis.com/auth/spreadsheets"),
    "slides": ("slides", "v1", "https://www.googleapis.com/auth/presentations"),
}
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
AIRTABLE_ALLOWED_OPERATION_ENV = "KEYSTONE_AIRTABLE_ALLOWED_OPERATION"
AIRTABLE_TEST_RECORD_MARKER = "KBA_TEST_RECORD"
AIRTABLE_SCHEMA_DETAIL_MAX_CHARS = 8_000
AIRTABLE_SCHEMA_DETAIL_MAX_FIELDS = 25
AIRTABLE_SCHEMA_DETAIL_MAX_TABLES = 25
AIRTABLE_READ_MAX_PAGES = 100
AIRTABLE_SCHEMA_RESOLUTION_MAX_PAGES = 100
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES_MIME_TYPE = "application/vnd.google-apps.presentation"
GOOGLE_DRIVE_MEDIA_DOWNLOAD_CHUNK_BYTES = 256 * 1024
GOOGLE_DRIVE_OCR_MAX_PIXELS = 40_000_000
GOOGLE_DRIVE_OCR_MAX_RASTER_BYTES = 100_000_000
GOOGLE_DOC_MAX_TABS = 200
GOOGLE_DOC_MAX_STRUCTURE_DEPTH = 24
GOOGLE_DOC_MAX_STRUCTURE_NODES = 20_000
GOOGLE_DOC_MAX_SEMANTIC_ANNOTATIONS = GOOGLE_DOC_MAX_STRUCTURE_NODES
GOOGLE_DOC_SEMANTIC_ANNOTATION_MAX_CHARS = 4_000
GOOGLE_SHEET_READ_MAX_ROWS = 1_000
GOOGLE_SHEET_READ_MAX_COLUMNS = 100
GOOGLE_SHEET_READ_MAX_CELLS = 100
GOOGLE_SHEET_PROJECTION_CHAR_BUDGET = 24_000
GOOGLE_SHEET_CELL_NOTE_PREVIEW_CHARS = 500
GOOGLE_SHEET_CELL_VALUE_PREVIEW_CHARS = 500
POWERPOINT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
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
    "google_drive_media_ocr_read",
    "google_slide_deck_read",
    "google_slide_deck_write",
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
GOOGLE_WORKSPACE_DELEGATED_TOOL_NAMES: tuple[str, ...] = tuple(
    name for name in GOOGLE_WORKSPACE_TOOL_NAMES if name != "google_drive_media_ocr_read"
)
GOOGLE_WORKSPACE_LIVE_READS_ENV = "KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS"


class GoogleWorkspaceScopeError(RuntimeError):
    """A non-retryable request for a Workspace service the token cannot access."""

    retryable = False
    error_code = "google_workspace_scope_missing"

    def __init__(self, service_name: str, required_scope: str) -> None:
        self.service_name = str(service_name or "").strip()
        self.required_scope = str(required_scope or "").strip()
        super().__init__(
            "Google Workspace authorization does not include the "
            f"{self.service_name} service required for this operation. "
            "Reauthorize that service before retrying."
        )

    def as_tool_result(self, *, operation: str) -> dict[str, Any]:
        return {
            "status": "blocked",
            "error_code": self.error_code,
            "operation": str(operation or "").strip(),
            "service": self.service_name,
            "retryable": False,
            "reason": str(self),
            "required_scope": self.required_scope,
            "send_enabled": False,
        }


class _LazyGoogleWorkspaceServices(dict[str, Any]):
    """Build only the Workspace service selected by the current typed tool."""

    def __init__(
        self,
        *,
        credentials: Any,
        build_service: Any,
        token_path: Path,
        refresh_request: Any,
    ) -> None:
        super().__init__()
        self._credentials = credentials
        self._build_service = build_service
        self._token_path = token_path
        self._refresh_request = refresh_request
        self._credentials_refreshed = False
        self._authorized_scopes = _google_workspace_authorized_scopes(
            credentials,
            token_path,
        )

    def __missing__(self, service_name: str) -> Any:
        spec = GOOGLE_WORKSPACE_SERVICE_SPECS.get(str(service_name or "").strip())
        if spec is None:
            raise KeyError(service_name)
        api_name, version, required_scope = spec
        if required_scope not in self._authorized_scopes:
            raise GoogleWorkspaceScopeError(service_name, required_scope)
        self._refresh_credentials_if_needed(service_name, required_scope)
        service = self._build_service(
            api_name,
            version,
            credentials=self._credentials,
            cache_discovery=False,
        )
        self[service_name] = service
        return service

    def _refresh_credentials_if_needed(
        self,
        service_name: str,
        required_scope: str,
    ) -> None:
        if self._credentials_refreshed or not self._credentials.expired:
            if not self._credentials.valid:
                raise RuntimeError("Google Workspace OAuth token is not valid.")
            return
        if not self._credentials.refresh_token:
            raise RuntimeError("Google Workspace OAuth token is expired and cannot be refreshed.")
        try:
            self._credentials.refresh(self._refresh_request())
        except Exception as exc:
            if "invalid_scope" in str(exc).lower():
                raise GoogleWorkspaceScopeError(service_name, required_scope) from exc
            raise
        self._credentials_refreshed = True
        self._token_path.write_text(self._credentials.to_json(), encoding="utf-8")
        if not self._credentials.valid:
            raise RuntimeError("Google Workspace OAuth token is not valid.")


def _airtable_live_reads_default() -> bool:
    return parse_bool(os.getenv(AIRTABLE_LIVE_READS_ENV))


def _google_workspace_live_reads_default() -> bool:
    return parse_bool(os.getenv(GOOGLE_WORKSPACE_LIVE_READS_ENV))


def _google_workspace_read_call(
    tool_name: str,
    operation: str,
    call: Any,
) -> dict[str, Any]:
    """Return a typed non-retryable blocker for a missing provider scope."""

    try:
        result = call()
    except GoogleWorkspaceScopeError as exc:
        result = exc.as_tool_result(operation=operation)
    record_provider_read_result(tool_name, result)
    return result


def google_workspace_tools(*, include_media_ocr: bool = False) -> list[Any]:
    """Return Google Workspace tools, reserving media OCR for its direct owners.

    Most agents receive bounded Drive, Docs, Slides, and Sheets capabilities and
    delegate image/PDF interpretation to the Workspace specialist.  Chief of
    Staff may opt in when a canonical Workspace plan identifies the exact file.
    """

    tools = [
        google_doc_read,
        google_doc_write,
        google_doc_trash,
        google_drive_list_folder,
        google_drive_search_files,
        google_drive_get_file_metadata,
        google_slide_deck_read,
        google_slide_deck_write,
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
    if include_media_ocr:
        tools.insert(6, google_drive_media_ocr_read)
    return tools


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
        result = extract_website_content_with_fallbacks(
            normalized_url,
            company_name="article",
            primary_provider=provider,
            guardrail_context="public_web_source",
            live=True,
            extractor=extract_website_content,
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
            "provider": "airtable",
            "operation": "read_schema",
            "provider_read": False,
            "request": _safe_request_preview(request),
            "schema": airtable_schema_preview_payload(summary),
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
        "provider": "airtable",
        "operation": "read_schema",
        "provider_read": True,
        "schema": airtable_schema_preview_payload(summary),
        "source_snapshot": {
            "sha256": summary.source_snapshot_sha256,
            "basis": "exact_airtable_metadata_response",
        },
        "send_enabled": False,
        "audit_notes": [
            (
                "Airtable metadata was reduced to bounded table and field previews; "
                "coverage and exact-detail requests identify omitted supported content."
            ),
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


def _airtable_schema_detail_next_request(
    *,
    base_alias: str,
    base_id: str,
    read_mode: str,
    table_id: str,
    field_id: str,
    table_start: int,
    max_tables: int,
    field_start: int,
    max_fields: int,
    start_char: int,
    max_chars: int,
    expected_source_sha256: str,
) -> dict[str, Any]:
    return {
        "base_alias": base_alias,
        "base_id": base_id,
        "read_mode": read_mode,
        "table_id": table_id,
        "field_id": field_id,
        "table_start": table_start,
        "max_tables": max_tables,
        "field_start": field_start,
        "max_fields": max_fields,
        "start_char": start_char,
        "max_chars": max_chars,
        "expected_source_sha256": expected_source_sha256,
    }


def _airtable_schema_field_index_item(
    field: Mapping[str, Any],
    *,
    base_id: str,
    table_id: str,
    source_snapshot_sha256: str,
) -> dict[str, Any]:
    summary = airtable_field_summary_from_metadata(
        field,
        base_id=base_id,
        table_id=table_id,
        source_snapshot_sha256=source_snapshot_sha256,
    )
    return {
        "field_id": summary.field_id,
        "name": summary.name,
        "field_type": summary.field_type,
        "field_mode": summary.field_mode,
        "is_computed": summary.is_computed,
        "is_manual": summary.is_manual,
        "validity": summary.validity,
        "is_valid": summary.is_valid,
        "description_available": "description" in field,
        "option_keys": sorted(
            str(key)
            for key in (field.get("options") if isinstance(field.get("options"), Mapping) else {})
        ),
        "option_value_states": summary.option_value_states,
    }


def airtable_read_schema_detail_impl(
    *,
    base_alias: str = "",
    base_id: str = "",
    read_mode: Literal[
        "table_index", "table_detail", "field_index", "field_detail"
    ] = "field_detail",
    table_id: str = "",
    field_id: str = "",
    table_start: int = 0,
    max_tables: int = AIRTABLE_SCHEMA_MAX_TABLES,
    field_start: int = 0,
    max_fields: int = AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE,
    start_char: int = 0,
    max_chars: int = 2_000,
    expected_source_sha256: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Read one bounded exact Airtable schema index/detail window."""

    config = _airtable_base_config(base_alias=base_alias, base_id=base_id)
    resolved_base_id = str(config.get("base_id") or "").strip()
    request = {
        "method": "GET",
        "url": _airtable_base_schema_url(resolved_base_id or "app_dry_run"),
        "params": {},
    }
    if not live:
        return {
            "status": "dry-run",
            "provider": "airtable",
            "operation": "read_schema_detail",
            "provider_read": False,
            "request": _safe_request_preview(request),
            "read_mode": read_mode,
            "table_id": table_id,
            "field_id": field_id,
            "send_enabled": False,
            "limitations": ["No Airtable metadata API call was made."],
        }

    _require_airtable_credentials(
        base_id=resolved_base_id,
        access_token=config["access_token"],
    )
    payload = _airtable_send(request, access_token=config["access_token"])
    source_sha256 = airtable_schema_snapshot_sha256(payload)
    source_snapshot = {
        "sha256": source_sha256,
        "expected_sha256": expected_source_sha256,
        "matches_expected": (
            source_sha256 == expected_source_sha256 if expected_source_sha256 else None
        ),
        "basis": "exact_airtable_metadata_response",
    }
    common = {
        "provider": "airtable",
        "operation": "read_schema_detail",
        "provider_read": True,
        "read_mode": read_mode,
        "base_id": resolved_base_id,
        "source_snapshot": source_snapshot,
        "send_enabled": False,
    }
    if expected_source_sha256 and source_sha256 != expected_source_sha256:
        return {
            **common,
            "status": "source_changed",
            "table_id": table_id,
            "field_id": field_id,
            "detail_text": "",
            "limitations": [
                "Airtable schema changed after the preview; restart discovery before reading detail."
            ],
            "continuation": {"available": False, "next_request": None},
        }

    raw_tables = payload.get("tables") if isinstance(payload, Mapping) else None
    tables = (
        [item for item in raw_tables if isinstance(item, Mapping)]
        if isinstance(raw_tables, Sequence) and not isinstance(raw_tables, str)
        else []
    )
    if read_mode == "table_index":
        bounded_start = max(0, int(table_start or 0))
        bounded_count = max(1, min(AIRTABLE_SCHEMA_DETAIL_MAX_TABLES, int(max_tables or 1)))
        page = tables[bounded_start : bounded_start + bounded_count]
        end = bounded_start + len(page)
        has_more = end < len(tables)
        next_request = (
            _airtable_schema_detail_next_request(
                base_alias=base_alias,
                base_id=resolved_base_id,
                read_mode="table_index",
                table_id="",
                field_id="",
                table_start=end,
                max_tables=bounded_count,
                field_start=0,
                max_fields=max_fields,
                start_char=0,
                max_chars=max_chars,
                expected_source_sha256=source_sha256,
            )
            if has_more
            else None
        )
        return {
            **common,
            "status": "success",
            "tables": [
                {
                    "table_id": str(item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "primary_field_id": str(item.get("primaryFieldId") or ""),
                    "field_count": len(item.get("fields") or []),
                }
                for item in page
            ],
            "coverage": {
                "unit": "table",
                "start": bounded_start,
                "end": end,
                "full_count": len(tables),
                "has_more": has_more,
                "complete": not has_more,
            },
            "continuation": {
                "available": has_more,
                "next_request": next_request,
            },
        }

    matched_tables = [item for item in tables if str(item.get("id") or "") == table_id]
    if len(matched_tables) != 1:
        return {
            **common,
            "status": "not_found" if not matched_tables else "ambiguous",
            "table_id": table_id,
            "field_id": field_id,
            "detail_text": "",
            "limitations": ["One exact Airtable table ID is required for schema detail reads."],
            "continuation": {"available": False, "next_request": None},
        }
    table = matched_tables[0]
    raw_fields = table.get("fields")
    fields = (
        [item for item in raw_fields if isinstance(item, Mapping)]
        if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, str)
        else []
    )
    table_identity = {
        "table_id": table_id,
        "name": str(table.get("name") or ""),
        "primary_field_id": str(table.get("primaryFieldId") or ""),
    }
    if read_mode == "table_detail":
        table_detail = {
            "id": table.get("id"),
            "name": table.get("name"),
            "description": table.get("description") if "description" in table else None,
            "primaryFieldId": table.get("primaryFieldId"),
        }
        serialized = json.dumps(
            table_detail,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        bounded_start = max(0, int(start_char or 0))
        bounded_chars = max(
            500,
            min(AIRTABLE_SCHEMA_DETAIL_MAX_CHARS, int(max_chars or 2_000)),
        )
        end = min(len(serialized), bounded_start + bounded_chars)
        has_more = end < len(serialized)
        next_request = (
            _airtable_schema_detail_next_request(
                base_alias=base_alias,
                base_id=resolved_base_id,
                read_mode="table_detail",
                table_id=table_id,
                field_id="",
                table_start=0,
                max_tables=max_tables,
                field_start=0,
                max_fields=max_fields,
                start_char=end,
                max_chars=bounded_chars,
                expected_source_sha256=source_sha256,
            )
            if has_more
            else None
        )
        return {
            **common,
            "status": "success",
            "table": table_identity,
            "detail_text": serialized[bounded_start:end],
            "read_window": {
                "unit": "serialized_exact_table_metadata_unicode_characters",
                "start": bounded_start,
                "end": end,
                "full_count": len(serialized),
                "has_more": has_more,
                "complete": not has_more,
            },
            "continuation": {
                "available": has_more,
                "next_request": next_request,
            },
            "limitations": ["This is exact provider metadata for one table, not record data."],
        }
    if read_mode == "field_index":
        bounded_start = max(0, int(field_start or 0))
        bounded_count = max(1, min(AIRTABLE_SCHEMA_DETAIL_MAX_FIELDS, int(max_fields or 1)))
        page = fields[bounded_start : bounded_start + bounded_count]
        end = bounded_start + len(page)
        has_more = end < len(fields)
        next_request = (
            _airtable_schema_detail_next_request(
                base_alias=base_alias,
                base_id=resolved_base_id,
                read_mode="field_index",
                table_id=table_id,
                field_id="",
                table_start=0,
                max_tables=max_tables,
                field_start=end,
                max_fields=bounded_count,
                start_char=0,
                max_chars=max_chars,
                expected_source_sha256=source_sha256,
            )
            if has_more
            else None
        )
        return {
            **common,
            "status": "success",
            "table": table_identity,
            "fields": [
                _airtable_schema_field_index_item(
                    item,
                    base_id=resolved_base_id,
                    table_id=table_id,
                    source_snapshot_sha256=source_sha256,
                )
                for item in page
            ],
            "coverage": {
                "unit": "field",
                "start": bounded_start,
                "end": end,
                "full_count": len(fields),
                "has_more": has_more,
                "complete": not has_more,
            },
            "continuation": {
                "available": has_more,
                "next_request": next_request,
            },
        }

    matched_fields = [item for item in fields if str(item.get("id") or "") == field_id]
    if len(matched_fields) != 1:
        return {
            **common,
            "status": "not_found" if not matched_fields else "ambiguous",
            "table": table_identity,
            "field_id": field_id,
            "detail_text": "",
            "limitations": ["One exact Airtable field ID in the selected table is required."],
            "continuation": {"available": False, "next_request": None},
        }
    field = matched_fields[0]
    serialized = json.dumps(
        field,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    bounded_start = max(0, int(start_char or 0))
    bounded_chars = max(500, min(AIRTABLE_SCHEMA_DETAIL_MAX_CHARS, int(max_chars or 2_000)))
    end = min(len(serialized), bounded_start + bounded_chars)
    has_more = end < len(serialized)
    next_request = (
        _airtable_schema_detail_next_request(
            base_alias=base_alias,
            base_id=resolved_base_id,
            read_mode="field_detail",
            table_id=table_id,
            field_id=field_id,
            table_start=0,
            max_tables=max_tables,
            field_start=0,
            max_fields=max_fields,
            start_char=end,
            max_chars=bounded_chars,
            expected_source_sha256=source_sha256,
        )
        if has_more
        else None
    )
    return {
        **common,
        "status": "success",
        "table": table_identity,
        "field": {
            "field_id": field_id,
            "name": str(field.get("name") or ""),
            "field_type": str(field.get("type") or ""),
        },
        "detail_text": serialized[bounded_start:end],
        "read_window": {
            "unit": "serialized_exact_field_unicode_characters",
            "start": bounded_start,
            "end": end,
            "full_count": len(serialized),
            "has_more": has_more,
            "complete": not has_more,
        },
        "continuation": {
            "available": has_more,
            "next_request": next_request,
        },
        "limitations": ["This is exact saved provider schema JSON for one field, not record data."],
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_read_schema_detail(
    base_alias: str = "",
    base_id: str = "",
    read_mode: Literal[
        "table_index", "table_detail", "field_index", "field_detail"
    ] = "field_detail",
    table_id: str = "",
    field_id: str = "",
    table_start: int = 0,
    max_tables: int = AIRTABLE_SCHEMA_MAX_TABLES,
    field_start: int = 0,
    max_fields: int = AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE,
    start_char: int = 0,
    max_chars: int = 2_000,
    expected_source_sha256: str = "",
    live: bool = False,
) -> str:
    """Read bounded exact Airtable schema indexes or one field's raw metadata."""

    return json.dumps(
        airtable_read_schema_detail_impl(
            base_alias=base_alias,
            base_id=base_id,
            read_mode=read_mode,
            table_id=table_id,
            field_id=field_id,
            table_start=table_start,
            max_tables=max_tables,
            field_start=field_start,
            max_fields=max_fields,
            start_char=start_char,
            max_chars=max_chars,
            expected_source_sha256=expected_source_sha256,
            live=live or _airtable_live_reads_default(),
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
    offset: str = "",
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
        params: dict[str, str | int] = {"pageSize": min(100, max(record_limit, 1))}
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
    initial_offset = str(offset or "").strip()
    if initial_offset:
        params["offset"] = initial_offset
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
            "offset": initial_offset,
            "pagination": {
                "status": "dry_run",
                "content_complete": False,
                "has_more": False,
                "next_offset": "",
            },
            "send_enabled": False,
        }
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])

    def merge_page_records(
        page_records: list[Any],
        *,
        records: list[Any],
        records_by_id: dict[str, Any],
        duplicate_record_ids: set[str],
        record_identity_conflicts: list[dict[str, Any]],
    ) -> tuple[int, bool]:
        remaining = max(record_limit - len(records), 0)
        new_identity_count = 0
        provider_page_overflow = False
        for record in page_records:
            if len(records) >= record_limit:
                provider_page_overflow = True
                break
            if isinstance(record, Mapping):
                record_id = str(record.get("id") or "")
                if record_id and record_id in records_by_id:
                    duplicate_record_ids.add(record_id)
                    if records_by_id[record_id] != record and len(record_identity_conflicts) < 10:
                        record_identity_conflicts.append(
                            {
                                "record_id": record_id,
                                "first_record": records_by_id[record_id],
                                "repeated_record": record,
                            }
                        )
                    continue
                if record_id:
                    records_by_id[record_id] = record
            records.append(record)
            new_identity_count += 1
        if len(page_records) > remaining:
            provider_page_overflow = True
        return new_identity_count, provider_page_overflow

    if fetch_all:
        records: list[Any] = []
        records_by_id: dict[str, Any] = {}
        duplicate_record_ids: set[str] = set()
        record_identity_conflicts: list[dict[str, Any]] = []
        current_offset = initial_offset
        seen_request_offsets: set[str] = set()
        page_count = 0
        next_offset = ""
        pagination_status = "complete"
        provider_page_overflow = False
        page_history: list[dict[str, Any]] = []
        while True:
            if current_offset and current_offset in seen_request_offsets:
                pagination_status = "repeated_offset"
                next_offset = current_offset
                break
            if current_offset:
                seen_request_offsets.add(current_offset)
            remaining = max(record_limit - len(records), 0)
            if remaining <= 0:
                pagination_status = "record_limit_reached"
                break
            page_request = {
                **request,
                "params": {
                    **params,
                    "pageSize": min(100, remaining),
                    **({"offset": current_offset} if current_offset else {}),
                },
            }
            payload = _airtable_send(page_request, access_token=config["access_token"])
            page_records = payload.get("records", []) if isinstance(payload, dict) else []
            if not isinstance(page_records, list):
                page_records = []
            new_identity_count, overflow = merge_page_records(
                page_records,
                records=records,
                records_by_id=records_by_id,
                duplicate_record_ids=duplicate_record_ids,
                record_identity_conflicts=record_identity_conflicts,
            )
            provider_page_overflow = provider_page_overflow or overflow
            page_count += 1
            provider_next_offset = (
                str(payload.get("offset") or "").strip() if isinstance(payload, dict) else ""
            )
            page_history.append(
                {
                    "page": page_count,
                    "request_offset": current_offset,
                    "provider_record_count": len(page_records),
                    "new_identity_count": new_identity_count,
                    "next_offset": provider_next_offset,
                }
            )
            if provider_next_offset and (
                provider_next_offset == current_offset
                or provider_next_offset in seen_request_offsets
            ):
                pagination_status = "repeated_offset"
                next_offset = provider_next_offset
                break
            if provider_page_overflow:
                pagination_status = "provider_page_exceeded_requested_bound"
                next_offset = provider_next_offset
                break
            if len(records) >= record_limit:
                next_offset = provider_next_offset
                pagination_status = "provider_has_more" if provider_next_offset else "complete"
                break
            if not provider_next_offset:
                pagination_status = "complete"
                next_offset = ""
                break
            if page_count >= AIRTABLE_READ_MAX_PAGES:
                pagination_status = "page_limit_reached"
                next_offset = provider_next_offset
                break
            current_offset = provider_next_offset
        has_more = bool(next_offset) or provider_page_overflow
        stalled = pagination_status in {"repeated_offset", "page_limit_reached"}
        identity_conflict = bool(record_identity_conflicts)
        if identity_conflict:
            pagination_status = "record_identity_conflict"
        truncated = has_more or stalled or identity_conflict
        next_request = (
            {
                "table": table_name,
                "base_alias": base_alias,
                "base_id": config["base_id"],
                "view": view,
                "filter_formula": filter_formula,
                "max_records": record_limit,
                "fetch_all": True,
                "offset": next_offset,
            }
            if next_offset and not stalled
            else None
        )
        return {
            "status": "success",
            "table": table_name,
            "records": records,
            "identity_fingerprints": identity_fingerprints(
                record.get("id")
                for record in records
                if isinstance(record, Mapping) and record.get("id")
            ),
            "fetch_all": True,
            "page_count": page_count,
            "item_count": len(records),
            "record_limit": record_limit,
            "truncated": truncated,
            "has_more": has_more,
            "next_offset": next_offset,
            "content_complete": not truncated,
            "duplicate_record_ids": sorted(duplicate_record_ids),
            "record_identity_conflicts": record_identity_conflicts,
            "pagination": {
                "status": pagination_status,
                "page_count": page_count,
                "max_pages": AIRTABLE_READ_MAX_PAGES,
                "content_complete": not truncated,
                "has_more": has_more,
                "next_offset": next_offset,
                "provider_page_overflow": provider_page_overflow,
                "duplicate_record_count": len(duplicate_record_ids),
                "identity_conflict_count": len(record_identity_conflicts),
                "page_history": page_history,
                "consistency": "provider_offset_chain_without_snapshot_isolation",
            },
            "continuation": {
                "available": next_request is not None,
                "next_request": next_request,
            },
            "limitations": (
                [
                    "The same Airtable record ID returned conflicting provider values; content was not treated as complete."
                ]
                if identity_conflict
                else [
                    "Provider pagination made no safe progress; repeated or conflicting pages were not treated as complete."
                ]
                if stalled
                else [
                    "Airtable record pagination does not provide snapshot isolation across pages."
                ]
                if page_count > 1
                else []
            ),
            "send_enabled": False,
        }
    payload = _airtable_send(request, access_token=config["access_token"])
    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        records = []
    unique_records: list[Any] = []
    records_by_id: dict[str, Any] = {}
    duplicate_record_ids: set[str] = set()
    record_identity_conflicts: list[dict[str, Any]] = []
    _new_identity_count, provider_page_overflow = merge_page_records(
        records,
        records=unique_records,
        records_by_id=records_by_id,
        duplicate_record_ids=duplicate_record_ids,
        record_identity_conflicts=record_identity_conflicts,
    )
    next_offset = str(payload.get("offset") or "").strip() if isinstance(payload, dict) else ""
    has_more = bool(next_offset) or provider_page_overflow
    identity_conflict = bool(record_identity_conflicts)
    content_complete = not has_more and not identity_conflict
    next_request = (
        {
            "table": table_name,
            "base_alias": base_alias,
            "base_id": config["base_id"],
            "view": view,
            "filter_formula": filter_formula,
            "max_records": record_limit,
            "fetch_all": False,
            "offset": next_offset,
        }
        if next_offset
        else None
    )
    return {
        "status": "success",
        "table": table_name,
        "records": unique_records,
        "identity_fingerprints": identity_fingerprints(
            record.get("id")
            for record in unique_records
            if isinstance(record, Mapping) and record.get("id")
        ),
        "fetch_all": False,
        "page_count": 1,
        "item_count": len(unique_records),
        "record_limit": record_limit,
        "truncated": not content_complete,
        "has_more": has_more,
        "next_offset": next_offset,
        "content_complete": content_complete,
        "duplicate_record_ids": sorted(duplicate_record_ids),
        "record_identity_conflicts": record_identity_conflicts,
        "pagination": {
            "status": (
                "record_identity_conflict"
                if identity_conflict
                else "provider_has_more"
                if has_more
                else "complete"
            ),
            "page_count": 1,
            "content_complete": content_complete,
            "has_more": has_more,
            "next_offset": next_offset,
            "provider_page_overflow": provider_page_overflow,
            "duplicate_record_count": len(duplicate_record_ids),
            "identity_conflict_count": len(record_identity_conflicts),
            "consistency": "single_provider_page",
        },
        "continuation": {
            "available": next_request is not None,
            "next_request": next_request,
        },
        "limitations": (
            [
                "The same Airtable record ID returned conflicting provider values; content was not treated as complete."
            ]
            if identity_conflict
            else []
        ),
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
    offset: str = "",
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
            offset=offset,
            live=live or _airtable_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


_AIRTABLE_ESTIMATED_PERIOD_WORDS = {
    "first": 1,
    "one": 1,
    "second": 2,
    "two": 2,
    "third": 3,
    "three": 3,
    "fourth": 4,
    "four": 4,
}


def _airtable_estimated_period_number(value: object) -> int | None:
    """Normalize one estimated-tax period reference without routing on prose."""

    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return None
    if text in {"current", "this", "current period", "this period"}:
        month = datetime.now(ZoneInfo("America/New_York")).month
        if month <= 3:
            return 1
        if month <= 5:
            return 2
        if month <= 8:
            return 3
        return 4
    direct = re.fullmatch(r"(?:q|period\s*)?([1-4])", text)
    if direct:
        return int(direct.group(1))
    for word, number in _AIRTABLE_ESTIMATED_PERIOD_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return number
    match = re.search(r"\b(?:q|period\s*)?([1-4])\b", text)
    return int(match.group(1)) if match else None


def _airtable_field_values(value: object) -> list[object]:
    if isinstance(value, list | tuple | set):
        return [item for item in value]
    if isinstance(value, Mapping):
        return [
            value.get(key) for key in ("name", "label", "value") if value.get(key) not in (None, "")
        ]
    return [value]


def _airtable_record_estimated_period(
    fields: Mapping[str, Any],
    *,
    period_field: str,
    date_field: str,
) -> int | None:
    if period_field:
        for value in _airtable_field_values(fields.get(period_field)):
            period = _airtable_estimated_period_number(value)
            if period is not None:
                return period
    if not date_field:
        return None
    value = str(fields.get(date_field) or "").strip()
    match = re.search(r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-\d{1,2}\b", value)
    if not match:
        match = re.search(r"\b(?P<month>\d{1,2})/\d{1,2}/(?P<year>20\d{2})\b", value)
    if not match:
        return None
    month = int(match.group("month"))
    if month <= 3:
        return 1
    if month <= 5:
        return 2
    if month <= 8:
        return 3
    return 4


def _airtable_record_year(fields: Mapping[str, Any], *, date_field: str) -> int | None:
    if not date_field:
        return None
    match = re.search(r"\b(20\d{2})\b", str(fields.get(date_field) or ""))
    return int(match.group(1)) if match else None


def _airtable_schema_table_fields(
    schema_result: Mapping[str, Any],
    *,
    table: str,
) -> list[str]:
    """Return fields from a complete preview; partial previews require resolution."""

    schema = schema_result.get("schema")
    raw_tables = schema.get("tables") if isinstance(schema, Mapping) else None
    if not isinstance(raw_tables, list):
        return []
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping):
            continue
        if str(raw_table.get("name") or "") != table:
            continue
        coverage = raw_table.get("fields_coverage")
        if isinstance(coverage, Mapping) and coverage.get("has_more") is True:
            return []
        raw_fields = raw_table.get("fields")
        if not isinstance(raw_fields, list):
            return []
        return [
            str(field.get("name") or "")
            for field in raw_fields
            if isinstance(field, Mapping) and str(field.get("name") or "")
        ]
    return []


def _airtable_schema_resolution_request_key(request: Mapping[str, Any]) -> str:
    return json.dumps(dict(request), ensure_ascii=False, sort_keys=True, default=str)


def _airtable_resolve_schema_table_fields(
    schema_result: Mapping[str, Any],
    *,
    table: str,
    base_alias: str,
    live: bool,
) -> dict[str, Any]:
    """Resolve one exact table and all field identities from bounded schema pages."""

    schema = schema_result.get("schema")
    if not isinstance(schema, Mapping):
        return {"status": "invalid_schema", "fields": [], "complete": False}
    source_snapshot_sha256 = str(
        schema.get("source_snapshot_sha256")
        or (
            schema_result.get("source_snapshot", {}).get("sha256")
            if isinstance(schema_result.get("source_snapshot"), Mapping)
            else ""
        )
        or ""
    )
    raw_tables = schema.get("tables")
    visible_tables = (
        [dict(item) for item in raw_tables if isinstance(item, Mapping)]
        if isinstance(raw_tables, list)
        else []
    )
    tables_by_id: dict[str, dict[str, Any]] = {}
    tables_without_id: list[dict[str, Any]] = []
    for item in visible_tables:
        item_id = str(item.get("table_id") or "")
        if item_id:
            tables_by_id[item_id] = item
        else:
            tables_without_id.append(item)

    detail_page_count = 0
    seen_requests: set[str] = set()
    coverage = schema.get("tables_coverage")
    request = (
        dict(coverage.get("next_request"))
        if isinstance(coverage, Mapping)
        and coverage.get("has_more") is True
        and isinstance(coverage.get("next_request"), Mapping)
        else None
    )
    while request is not None and detail_page_count < AIRTABLE_SCHEMA_RESOLUTION_MAX_PAGES:
        request.setdefault("base_alias", base_alias)
        request["live"] = live
        request_key = _airtable_schema_resolution_request_key(request)
        if request_key in seen_requests:
            return {
                "status": "pagination_stalled",
                "fields": [],
                "complete": False,
                "source_snapshot_sha256": source_snapshot_sha256,
                "detail_page_count": detail_page_count,
            }
        seen_requests.add(request_key)
        page = airtable_read_schema_detail_impl(**request)
        detail_page_count += 1
        if page.get("status") != "success":
            return {
                "status": str(page.get("status") or "detail_read_failed"),
                "fields": [],
                "complete": False,
                "source_snapshot_sha256": source_snapshot_sha256,
                "detail_page_count": detail_page_count,
            }
        for item in page.get("tables", []):
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("table_id") or "")
            if item_id:
                tables_by_id[item_id] = dict(item)
            else:
                tables_without_id.append(dict(item))
        continuation = page.get("continuation")
        request = (
            dict(continuation.get("next_request"))
            if isinstance(continuation, Mapping)
            and continuation.get("available") is True
            and isinstance(continuation.get("next_request"), Mapping)
            else None
        )
    if request is not None:
        return {
            "status": "page_limit_reached",
            "fields": [],
            "complete": False,
            "source_snapshot_sha256": source_snapshot_sha256,
            "detail_page_count": detail_page_count,
        }

    candidate_tables = [
        item
        for item in [*tables_by_id.values(), *tables_without_id]
        if str(item.get("name") or "") == table
    ]
    if len(candidate_tables) != 1:
        return {
            "status": "table_not_found" if not candidate_tables else "table_ambiguous",
            "fields": [],
            "complete": True,
            "source_snapshot_sha256": source_snapshot_sha256,
            "detail_page_count": detail_page_count,
            "matching_table_ids": [str(item.get("table_id") or "") for item in candidate_tables],
        }
    selected_table = candidate_tables[0]
    table_id = str(selected_table.get("table_id") or "")
    raw_fields = selected_table.get("fields")
    visible_fields = (
        [dict(item) for item in raw_fields if isinstance(item, Mapping)]
        if isinstance(raw_fields, list)
        else []
    )
    fields_by_id: dict[str, dict[str, Any]] = {}
    fields_without_id: list[dict[str, Any]] = []
    for item in visible_fields:
        item_id = str(item.get("field_id") or "")
        if item_id:
            fields_by_id[item_id] = item
        else:
            fields_without_id.append(item)

    field_coverage = selected_table.get("fields_coverage")
    if isinstance(field_coverage, Mapping) and field_coverage.get("has_more") is True:
        next_request = field_coverage.get("next_request")
        request = dict(next_request) if isinstance(next_request, Mapping) else None
    elif table_id and not visible_fields and "fields" not in selected_table:
        request = {
            "base_alias": base_alias,
            "base_id": str(schema.get("base_id") or ""),
            "read_mode": "field_index",
            "table_id": table_id,
            "field_start": 0,
            "max_fields": AIRTABLE_SCHEMA_MAX_FIELDS_PER_TABLE,
            "expected_source_sha256": source_snapshot_sha256,
            "live": live,
        }
    else:
        request = None
    while request is not None and detail_page_count < AIRTABLE_SCHEMA_RESOLUTION_MAX_PAGES:
        request.setdefault("base_alias", base_alias)
        request["live"] = live
        request_key = _airtable_schema_resolution_request_key(request)
        if request_key in seen_requests:
            return {
                "status": "pagination_stalled",
                "fields": [],
                "complete": False,
                "table_id": table_id,
                "source_snapshot_sha256": source_snapshot_sha256,
                "detail_page_count": detail_page_count,
            }
        seen_requests.add(request_key)
        page = airtable_read_schema_detail_impl(**request)
        detail_page_count += 1
        if page.get("status") != "success":
            return {
                "status": str(page.get("status") or "detail_read_failed"),
                "fields": [],
                "complete": False,
                "table_id": table_id,
                "source_snapshot_sha256": source_snapshot_sha256,
                "detail_page_count": detail_page_count,
            }
        for item in page.get("fields", []):
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("field_id") or "")
            if item_id:
                existing = fields_by_id.get(item_id)
                if existing is not None and existing != item:
                    return {
                        "status": "field_identity_conflict",
                        "fields": [],
                        "complete": False,
                        "table_id": table_id,
                        "source_snapshot_sha256": source_snapshot_sha256,
                        "detail_page_count": detail_page_count,
                    }
                fields_by_id[item_id] = dict(item)
            else:
                fields_without_id.append(dict(item))
        continuation = page.get("continuation")
        request = (
            dict(continuation.get("next_request"))
            if isinstance(continuation, Mapping)
            and continuation.get("available") is True
            and isinstance(continuation.get("next_request"), Mapping)
            else None
        )
    if request is not None:
        return {
            "status": "page_limit_reached",
            "fields": [],
            "complete": False,
            "table_id": table_id,
            "source_snapshot_sha256": source_snapshot_sha256,
            "detail_page_count": detail_page_count,
        }
    return {
        "status": "success",
        "base_id": str(schema.get("base_id") or ""),
        "table_id": table_id,
        "table_name": str(selected_table.get("name") or ""),
        "fields": [*fields_by_id.values(), *fields_without_id],
        "complete": True,
        "source_snapshot_sha256": source_snapshot_sha256,
        "detail_page_count": detail_page_count,
    }


def _airtable_read_exact_schema_field(
    schema_resolution: Mapping[str, Any],
    *,
    field_name: str,
    base_alias: str,
    live: bool,
) -> dict[str, Any]:
    """Read one exact field's provider metadata through bounded snapshot-pinned pages."""

    raw_fields = schema_resolution.get("fields")
    fields = (
        [item for item in raw_fields if isinstance(item, Mapping)]
        if isinstance(raw_fields, list)
        else []
    )
    candidates = [item for item in fields if item.get("name") == field_name]
    if len(candidates) != 1:
        return {
            "status": "not_found" if not candidates else "ambiguous",
            "complete": False,
            "field_name": field_name,
        }
    field = candidates[0]
    field_id = str(field.get("field_id") or "")
    table_id = str(schema_resolution.get("table_id") or "")
    source_snapshot_sha256 = str(schema_resolution.get("source_snapshot_sha256") or "")
    if not field_id or not table_id or not source_snapshot_sha256:
        return {
            "status": "identity_unavailable",
            "complete": False,
            "field_name": field_name,
            "field": dict(field),
        }
    request: dict[str, Any] | None = {
        "base_alias": base_alias,
        "base_id": str(schema_resolution.get("base_id") or ""),
        "read_mode": "field_detail",
        "table_id": table_id,
        "field_id": field_id,
        "start_char": 0,
        "max_chars": AIRTABLE_SCHEMA_DETAIL_MAX_CHARS,
        "expected_source_sha256": source_snapshot_sha256,
        "live": live,
    }
    chunks: list[str] = []
    page_count = 0
    seen_requests: set[str] = set()
    while request is not None and page_count < AIRTABLE_SCHEMA_RESOLUTION_MAX_PAGES:
        request_key = _airtable_schema_resolution_request_key(request)
        if request_key in seen_requests:
            return {
                "status": "pagination_stalled",
                "complete": False,
                "field_id": field_id,
                "field_name": field_name,
                "page_count": page_count,
            }
        seen_requests.add(request_key)
        page = airtable_read_schema_detail_impl(**request)
        page_count += 1
        if page.get("status") != "success":
            return {
                "status": str(page.get("status") or "detail_read_failed"),
                "complete": False,
                "field_id": field_id,
                "field_name": field_name,
                "page_count": page_count,
            }
        chunks.append(str(page.get("detail_text") or ""))
        continuation = page.get("continuation")
        request = (
            dict(continuation.get("next_request"))
            if isinstance(continuation, Mapping)
            and continuation.get("available") is True
            and isinstance(continuation.get("next_request"), Mapping)
            else None
        )
        if request is not None:
            request.setdefault("base_alias", base_alias)
            request["live"] = live
    if request is not None:
        return {
            "status": "page_limit_reached",
            "complete": False,
            "field_id": field_id,
            "field_name": field_name,
            "page_count": page_count,
        }
    try:
        metadata = json.loads("".join(chunks))
    except json.JSONDecodeError:
        return {
            "status": "invalid_detail_json",
            "complete": False,
            "field_id": field_id,
            "field_name": field_name,
            "page_count": page_count,
        }
    if (
        not isinstance(metadata, Mapping)
        or str(metadata.get("id") or "") != field_id
        or str(metadata.get("name") or "") != field_name
    ):
        return {
            "status": "identity_mismatch",
            "complete": False,
            "field_id": field_id,
            "field_name": field_name,
            "page_count": page_count,
        }
    return {
        "status": "success",
        "complete": True,
        "field_id": field_id,
        "field_name": field_name,
        "source_snapshot_sha256": source_snapshot_sha256,
        "page_count": page_count,
        "metadata": dict(metadata),
    }


def _first_schema_field(fields: list[str], candidates: tuple[str, ...]) -> str:
    by_normalized = {" ".join(field.lower().split()): field for field in fields}
    for candidate in candidates:
        found = by_normalized.get(" ".join(candidate.lower().split()))
        if found:
            return found
    return ""


def _airtable_money_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    cleaned = str(value).strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _airtable_aggregate_unit(
    field_metadata_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Return source-declared numeric unit semantics without guessing currency codes."""

    metadata = field_metadata_result.get("metadata")
    if not isinstance(metadata, Mapping):
        field = field_metadata_result.get("field")
        field_summary = field if isinstance(field, Mapping) else {}
        return {
            "kind": "unknown",
            "field_type": str(field_summary.get("field_type") or ""),
            "symbol": "",
            "currency_code": "",
            "precision": None,
            "source": "schema_preview_without_exact_options",
        }
    field_type = str(metadata.get("type") or "")
    raw_options = metadata.get("options")
    options = raw_options if isinstance(raw_options, Mapping) else {}
    result = options.get("result")
    if isinstance(result, Mapping):
        numeric_type = str(result.get("type") or field_type)
        raw_numeric_options = result.get("options")
        numeric_options = raw_numeric_options if isinstance(raw_numeric_options, Mapping) else {}
        source = "airtable_computed_result_options"
    else:
        numeric_type = field_type
        numeric_options = options
        source = "airtable_field_options"
    raw_precision = numeric_options.get("precision")
    precision = (
        int(raw_precision)
        if isinstance(raw_precision, int)
        and not isinstance(raw_precision, bool)
        and 0 <= raw_precision <= 20
        else None
    )
    symbol = str(numeric_options.get("symbol") or "")
    kind = (
        "currency_symbol"
        if numeric_type == "currency"
        else "unitless_number"
        if numeric_type == "number"
        else "percent_ratio"
        if numeric_type == "percent"
        else "unknown"
    )
    return {
        "kind": kind,
        "field_type": field_type,
        "numeric_type": numeric_type,
        "symbol": symbol,
        "currency_code": "",
        "precision": precision,
        "source": source,
    }


def _airtable_format_aggregate_decimal(
    value: Decimal,
    *,
    unit: Mapping[str, Any],
) -> str:
    raw_precision = unit.get("precision")
    if isinstance(raw_precision, int) and not isinstance(raw_precision, bool):
        quantum = Decimal(1).scaleb(-raw_precision)
        return format(value.quantize(quantum), "f")
    return format(value, "f")


def _airtable_display_aggregate_decimal(
    value: Decimal,
    *,
    unit: Mapping[str, Any],
) -> str:
    numeric_text = _airtable_format_aggregate_decimal(value, unit=unit)
    symbol = str(unit.get("symbol") or "")
    if unit.get("kind") == "currency_symbol" and symbol:
        if numeric_text.startswith("-"):
            return f"-{symbol}{numeric_text[1:]}"
        return f"{symbol}{numeric_text}"
    return numeric_text


_AIRTABLE_RECORD_LABEL_TERMS = (
    "item",
    "description",
    "name",
    "title",
    "merchant",
    "vendor",
    "payee",
    "category",
    "type",
    "expense",
)


def _airtable_scalar_text(value: object) -> str:
    """Return one short display-safe scalar value from an Airtable field."""

    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float | Decimal | str):
        return " ".join(str(value).split())[:240]
    return ""


def _airtable_matching_record_summary(
    fields: Mapping[str, Any],
    *,
    amount_field: str,
    amount_unit: Mapping[str, Any],
    period_field: str,
    date_field: str,
) -> dict[str, str]:
    """Project one finance row without attachments or raw provider identity."""

    reserved = {amount_field, period_field, date_field}
    labels: list[tuple[str, str]] = []
    for term in _AIRTABLE_RECORD_LABEL_TERMS:
        for field_name, raw_value in fields.items():
            clean_name = str(field_name or "").strip()
            if (
                clean_name in reserved
                or any(existing_name == clean_name for existing_name, _ in labels)
                or term not in clean_name.casefold()
            ):
                continue
            candidate = _airtable_scalar_text(raw_value)
            if candidate:
                labels.append((clean_name, candidate))
                break
        if len(labels) >= 3:
            break
    date_value = _airtable_scalar_text(fields.get(date_field)) if date_field else ""
    amount = _airtable_money_decimal(fields.get(amount_field))
    amount_value = (
        _airtable_display_aggregate_decimal(amount, unit=amount_unit) if amount is not None else ""
    )
    key = (
        labels[0][1] if labels else (f"Expense on {date_value}" if date_value else "Expense record")
    )
    values = [f"{field_name}: {value}" for field_name, value in labels[1:]]
    if date_field and date_value:
        values.append(f"{date_field}: {date_value}")
    if amount_field and amount_value:
        values.append(f"{amount_field}: {amount_value}")
    period_value = _airtable_scalar_text(fields.get(period_field)) if period_field else ""
    note = f"{period_field}: {period_value}" if period_field and period_value else ""
    return {"key": key, "value": "; ".join(values), "note": note}


def airtable_aggregate_records_impl(
    *,
    table: str,
    base_alias: str = "finance_tax_tracker",
    amount_field: str = "",
    estimated_period: str = "",
    period_field: str = "",
    year: int = 0,
    date_field: str = "",
    max_records: int = 0,
    include_matching_records: bool = False,
    expected_record_ids: Sequence[str] | None = None,
    expected_total: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Schema-validate, filter, and deterministically sum one Airtable table."""

    clean_table = str(table or "").strip()
    if not clean_table:
        raise ValueError("table is required for a bounded Airtable aggregate")
    clean_alias = str(base_alias or "finance_tax_tracker").strip()
    requested_period = _airtable_estimated_period_number(estimated_period)
    if str(estimated_period or "").strip() and requested_period is None:
        raise ValueError("estimated_period must resolve to one of periods 1 through 4")
    requested_year = int(year or datetime.now(ZoneInfo("America/New_York")).year)
    schema = airtable_get_base_schema_impl(base_alias=clean_alias, live=live)
    schema_resolution = _airtable_resolve_schema_table_fields(
        schema,
        table=clean_table,
        base_alias=clean_alias,
        live=live,
    )
    raw_resolved_fields = schema_resolution.get("fields")
    resolved_field_summaries = (
        [item for item in raw_resolved_fields if isinstance(item, Mapping)]
        if isinstance(raw_resolved_fields, list)
        else []
    )
    fields = [
        str(item.get("name") or "")
        for item in resolved_field_summaries
        if str(item.get("name") or "")
    ]
    compact_schema_resolution = {
        "status": str(schema_resolution.get("status") or "unknown"),
        "complete": schema_resolution.get("complete") is True,
        "table_id": str(schema_resolution.get("table_id") or ""),
        "table_name": str(schema_resolution.get("table_name") or clean_table),
        "source_snapshot_sha256": str(schema_resolution.get("source_snapshot_sha256") or ""),
        "field_count": len(resolved_field_summaries),
        "detail_page_count": int(schema_resolution.get("detail_page_count") or 0),
    }
    if live and schema_resolution.get("status") != "success":
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "schema_resolution_incomplete",
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    resolved_amount_field = str(amount_field or "").strip() or _first_schema_field(
        fields,
        ("Total Expenses", "Total Expense", "Amount", "Total Paid"),
    )
    resolved_period_field = str(period_field or "").strip() or _first_schema_field(
        fields,
        ("Estimated Tax Periods", "Estimated Tax Period", "Quarter", "Tax Period"),
    )
    resolved_date_field = str(date_field or "").strip() or _first_schema_field(
        fields,
        ("Date of Expense", "Expense Date", "Payment Date", "Date"),
    )
    missing_fields = [
        field
        for field in (resolved_amount_field, resolved_period_field, resolved_date_field)
        if field and fields and field not in fields
    ]
    if missing_fields or (live and not resolved_amount_field):
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "requested_fields_not_found",
            "missing_fields": missing_fields or ["numeric amount field"],
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    requested_field_names = {
        field
        for field in (resolved_amount_field, resolved_period_field, resolved_date_field)
        if field
    }
    ambiguous_field_names = sorted(
        field_name
        for field_name in requested_field_names
        if sum(item.get("name") == field_name for item in resolved_field_summaries) > 1
    )
    if live and ambiguous_field_names:
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "requested_field_identity_ambiguous",
            "ambiguous_fields": ambiguous_field_names,
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    unsupported_field_names = sorted(
        str(item.get("name") or "")
        for item in resolved_field_summaries
        if item.get("name") in requested_field_names
        and (item.get("field_mode") == "unknown" or item.get("validity") == "invalid")
    )
    if live and unsupported_field_names:
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "requested_field_semantics_unsupported",
            "unsupported_fields": unsupported_field_names,
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    if live:
        amount_field_metadata = _airtable_read_exact_schema_field(
            schema_resolution,
            field_name=resolved_amount_field,
            base_alias=clean_alias,
            live=True,
        )
        if amount_field_metadata.get("status") not in {
            "success",
            "identity_unavailable",
        }:
            return {
                "status": "blocked",
                "operation": "aggregate_records",
                "table": clean_table,
                "reason": "amount_field_metadata_incomplete",
                "amount_field": resolved_amount_field,
                "field_metadata_status": str(amount_field_metadata.get("status") or "unknown"),
                "schema_resolution": compact_schema_resolution,
                "send_enabled": False,
            }
    else:
        amount_field_metadata = {
            "status": "identity_unavailable",
            "complete": False,
            "field_name": resolved_amount_field,
        }
    aggregate_unit = _airtable_aggregate_unit(amount_field_metadata)
    compact_schema_resolution["amount_field_metadata_status"] = str(
        amount_field_metadata.get("status") or "unknown"
    )
    compact_schema_resolution["amount_field_detail_page_count"] = int(
        amount_field_metadata.get("page_count") or 0
    )
    if live and requested_year and not resolved_date_field:
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "year_filter_not_resolvable_from_provider_schema",
            "year": requested_year,
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    if not live:
        return {
            "status": "dry-run",
            "operation": "aggregate_records",
            "base_alias": clean_alias,
            "table": clean_table,
            "amount_field": resolved_amount_field or amount_field,
            "estimated_period": requested_period,
            "period_field": resolved_period_field or period_field,
            "year": requested_year,
            "date_field": resolved_date_field or date_field,
            "fetch_all": True,
            "record_limit": int(max_records or 0),
            "include_matching_records": bool(include_matching_records),
            "unit": aggregate_unit,
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    read = airtable_read_records_impl(
        clean_table,
        base_alias=clean_alias,
        max_records=max_records,
        fetch_all=True,
        live=True,
    )
    record_identity_conflicts = read.get("record_identity_conflicts")
    if isinstance(record_identity_conflicts, list) and record_identity_conflicts:
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "record_identity_conflict",
            "conflicting_record_ids": sorted(
                {
                    str(item.get("record_id") or "")
                    for item in record_identity_conflicts
                    if isinstance(item, Mapping) and item.get("record_id")
                }
            ),
            "record_pagination": read.get("pagination"),
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    records = read.get("records") if isinstance(read, Mapping) else None
    if not isinstance(records, list):
        records = []
    total = Decimal("0")
    matching_records = 0
    contributing_records = 0
    period_evidence_count = 0
    matching_record_ids: list[str] = []
    matching_record_summaries: list[dict[str, str]] = []
    for record in records:
        raw_fields = record.get("fields") if isinstance(record, Mapping) else None
        if not isinstance(raw_fields, Mapping):
            continue
        record_period = _airtable_record_estimated_period(
            raw_fields,
            period_field=resolved_period_field,
            date_field=resolved_date_field,
        )
        if record_period is not None:
            period_evidence_count += 1
        if requested_period is not None and record_period != requested_period:
            continue
        record_year = _airtable_record_year(raw_fields, date_field=resolved_date_field)
        if requested_year and record_year != requested_year:
            continue
        amount = _airtable_money_decimal(raw_fields.get(resolved_amount_field))
        if amount is None:
            continue
        matching_records += 1
        total += amount
        record_id = str(record.get("id") or "").strip()
        if record_id and record_id not in matching_record_ids:
            matching_record_ids.append(record_id)
        if include_matching_records and len(matching_record_summaries) < 10:
            matching_record_summaries.append(
                _airtable_matching_record_summary(
                    raw_fields,
                    amount_field=resolved_amount_field,
                    amount_unit=aggregate_unit,
                    period_field=resolved_period_field,
                    date_field=resolved_date_field,
                )
            )
        if amount != Decimal("0"):
            contributing_records += 1
    if requested_period is not None and records and period_evidence_count == 0:
        return {
            "status": "blocked",
            "operation": "aggregate_records",
            "table": clean_table,
            "reason": "estimated_period_not_resolvable_from_provider_fields",
            "amount_field": resolved_amount_field,
            "period_field": resolved_period_field,
            "date_field": resolved_date_field,
            "schema_resolution": compact_schema_resolution,
            "send_enabled": False,
        }
    exact_total_text = format(total, "f")
    total_text = _airtable_format_aggregate_decimal(total, unit=aggregate_unit)
    display_total = _airtable_display_aggregate_decimal(total, unit=aggregate_unit)
    expected_ids = {
        str(item or "").strip() for item in (expected_record_ids or ()) if str(item or "").strip()
    }
    scope_membership_match = set(matching_record_ids) == expected_ids if expected_ids else None
    expected_total_decimal = _airtable_money_decimal(expected_total)
    prior_total_match = (
        total == expected_total_decimal if expected_total_decimal is not None else None
    )
    verification_passed = (
        read.get("content_complete") is not False
        and not bool(read.get("truncated"))
        and not bool(record_identity_conflicts)
    )
    result = {
        "status": "success",
        "operation": "aggregate_records",
        "provider": "airtable",
        "provider_read": True,
        "provider_write": False,
        "complete": verification_passed,
        "verified": verification_passed,
        "base_alias": clean_alias,
        "table": clean_table,
        "amount_field": resolved_amount_field,
        "estimated_period": requested_period,
        "period_field": resolved_period_field,
        "year": requested_year,
        "date_field": resolved_date_field,
        "total": total_text,
        "exact_total": exact_total_text,
        "display_total": display_total,
        "currency": str(aggregate_unit.get("currency_code") or ""),
        "unit": aggregate_unit,
        "matching_records": matching_records,
        "contributing_records": contributing_records,
        "records_checked": len(records),
        "record_limit": read.get("record_limit"),
        "truncated": bool(read.get("truncated")),
        "verification": {
            "status": "verified" if verification_passed else "incomplete",
            "passed": verification_passed,
            "schema_read": True,
            "records_read": True,
            "period_filter_applied": requested_period is not None,
            "deterministic_arithmetic": "decimal_sum",
            "exact_decimal_sum": exact_total_text,
            "unit_source_verified": amount_field_metadata.get("status") == "success",
            "scope_membership_match": scope_membership_match,
            "prior_total_match": prior_total_match,
            "record_projection_count": len(matching_record_summaries),
        },
        "result_scope": {
            "base_alias": clean_alias,
            "table": clean_table,
            "amount_field": resolved_amount_field,
            "estimated_period": requested_period,
            "period_field": resolved_period_field,
            "year": requested_year,
            "date_field": resolved_date_field,
            "total": exact_total_text,
            "presentation_total": total_text,
            "display_total": display_total,
            "currency": str(aggregate_unit.get("currency_code") or ""),
            "unit": aggregate_unit,
            "item_refs": matching_record_ids,
        },
        "schema_resolution": compact_schema_resolution,
        "send_enabled": False,
    }
    if include_matching_records:
        result["matching_record_summaries"] = matching_record_summaries
    return result


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_aggregate_records(
    table: str,
    base_alias: str = "finance_tax_tracker",
    amount_field: str = "",
    estimated_period: str = "",
    period_field: str = "",
    year: int = 0,
    date_field: str = "",
    max_records: int = 0,
    include_matching_records: bool = False,
    live: bool = False,
) -> str:
    """Return a schema-verified deterministic sum for one bounded Airtable table.

    Use for total/count questions instead of reading raw records and doing model
    arithmetic. `estimated_period` accepts current/this, 1-4, Q1-Q4, and ordinal
    wording. The tool resolves provider field names from schema, fetches the one
    selected table under AIRTABLE_READ_ALL_MAX_RECORDS, filters the requested
    estimated-tax period/year, and returns no raw transaction rows.
    """

    return json.dumps(
        airtable_aggregate_records_impl(
            table=table,
            base_alias=base_alias,
            amount_field=amount_field,
            estimated_period=estimated_period,
            period_field=period_field,
            year=year,
            date_field=date_field,
            max_records=max_records,
            include_matching_records=include_matching_records,
            live=live or _airtable_live_reads_default(),
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@durable_provider_tool("airtable_write_record")
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
    canonical_expense_period: int | None = None
    if table_name in {"Business Expenses", "Personal Expenses"} and (
        "Estimated Tax Periods" in fields
    ):
        canonical_expense_period = _airtable_estimated_period_number(
            fields.get("Estimated Tax Periods")
        )
        if canonical_expense_period is None:
            return {
                "status": "blocked",
                "reason": "Estimated Tax Periods must resolve to one of 1, 2, 3, or 4.",
                "operation": str(operation or "create").strip().lower(),
                "table": table_name,
                "send_enabled": False,
            }
        fields = dict(fields)
        fields["Estimated Tax Periods"] = str(canonical_expense_period)
    clean_record_id = record_id.strip()
    clean_operation = str(operation or "create").strip().lower()
    clean_match_filter = match_filter_formula.strip()
    provider_fields = fields
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
    allowed_operation = os.getenv(AIRTABLE_ALLOWED_OPERATION_ENV, "").strip().lower()
    if allowed_operation and clean_operation != allowed_operation:
        return {
            "status": "blocked",
            "reason": (
                "Airtable write operation did not match the operator-approved "
                f"{allowed_operation!r} scope."
            ),
            "operation": clean_operation,
            "allowed_operation": allowed_operation,
            "table": table_name,
            "send_enabled": False,
        }
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
    period_context: dict[str, Any] = {
        "status": "not_required",
        "canonical_value": "",
        "provider_read": False,
    }
    if canonical_expense_period is not None:
        period_context = _airtable_expense_period_context(
            table_name,
            base_alias=config["base_alias"] or base_alias,
            base_id=config["base_id"],
            canonical_period=str(canonical_expense_period),
        )
        if period_context.get("status") != "verified":
            return {
                "status": "blocked",
                "operation": clean_operation,
                "reason": str(
                    period_context.get("reason")
                    or "The existing Airtable tax-period values could not be verified."
                ),
                "table": table_name,
                "period_context": period_context,
                "send_enabled": False,
            }
    payload = _airtable_send(request, access_token=config["access_token"])
    written_record_id = str(payload.get("id") or clean_record_id).strip()
    if written_record_id:
        record_provider_observation(
            {
                "status": "observed",
                "provider": "airtable",
                "operation": "update" if clean_record_id else "create",
                "record_id": written_record_id,
                "provider_write": True,
                "verification": {"passed": False, "status": "pending_readback"},
            }
        )
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
        "base_alias": str(config.get("base_alias") or base_alias or "").strip(),
        "table": table_name,
        "record_id": written_record_id,
        "provider_link": (
            _airtable_record_provider_link(config["base_id"], written_record_id)
            if verification["passed"]
            else ""
        ),
        "record": payload,
        "verified_record": verified_record,
        "verification": verification,
        "approval_reference": approval_reference.strip(),
        "schema_validation": schema_validation,
        "period_context": period_context,
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


def airtable_reconcile_duplicate_expense_impl(
    keep_record_id: str,
    duplicate_record_id: str,
    *,
    target_estimated_tax_period: str,
    table: str = "Personal Expenses",
    base_alias: str = "finance_tax_tracker",
    base_id: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Correct one expense in place and remove one provider-verified duplicate."""

    keep_id = keep_record_id.strip()
    duplicate_id = duplicate_record_id.strip()
    period = target_estimated_tax_period.strip()
    if not keep_id or not duplicate_id or keep_id == duplicate_id:
        raise ValueError("Duplicate cleanup requires two distinct exact Airtable record IDs.")
    if period not in {"1", "2", "3", "4"}:
        raise ValueError("target_estimated_tax_period must be one of 1, 2, 3, or 4.")
    config = _airtable_base_config(
        base_alias=_infer_airtable_base_alias(
            base_alias=base_alias,
            base_id=base_id,
            table=table,
        ),
        base_id=base_id,
    )
    table_name = _airtable_table(table, config=config)
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if dry_run:
        return {
            "status": "dry-run",
            "operation": "reconcile_duplicate_expense",
            "table": table_name,
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "target_estimated_tax_period": period,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "audit_notes": [
                "No Airtable record was changed or deleted.",
                "Live execution requires exact duplicate proof before correction and cleanup.",
            ],
        }
    if not approval_reference.strip():
        raise RuntimeError("Airtable duplicate cleanup requires a non-empty approval_reference.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
        raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
    if not parse_bool(os.getenv("AIRTABLE_ALLOW_DUPLICATE_CLEANUP")):
        raise RuntimeError(
            "Airtable duplicate cleanup is disabled. "
            "Set AIRTABLE_ALLOW_DUPLICATE_CLEANUP=true for the approved cleanup window."
        )
    _require_airtable_credentials(base_id=config["base_id"], access_token=config["access_token"])

    def read_exact(record_id: str) -> list[dict[str, Any]]:
        result = airtable_read_records_impl(
            table_name,
            base_alias=base_alias,
            base_id=config["base_id"],
            filter_formula=f"RECORD_ID()='{record_id}'",
            max_records=2,
            live=True,
        )
        records = result.get("records", [])
        return (
            [record for record in records if isinstance(record, dict)]
            if isinstance(records, list)
            else []
        )

    keep_records = read_exact(keep_id)
    duplicate_records = read_exact(duplicate_id)
    if len(keep_records) != 1 or len(duplicate_records) != 1:
        return {
            "status": "blocked",
            "operation": "reconcile_duplicate_expense",
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "reason": "Both exact Airtable records must exist uniquely before cleanup.",
            "keep_records_found": len(keep_records),
            "duplicate_records_found": len(duplicate_records),
            "send_enabled": False,
        }
    keep_fields = keep_records[0].get("fields", {})
    duplicate_fields = duplicate_records[0].get("fields", {})
    if not isinstance(keep_fields, dict) or not isinstance(duplicate_fields, dict):
        return {
            "status": "blocked",
            "operation": "reconcile_duplicate_expense",
            "reason": "Airtable duplicate fields were unavailable.",
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "send_enabled": False,
        }
    identity_fields = (
        "Date of Expense",
        "Expense Client/Vendor",
        "Description",
        "Amount",
        "Total Expenses",
        "Receipt Available",
    )
    mismatched_identity_fields = [
        name for name in identity_fields if keep_fields.get(name) != duplicate_fields.get(name)
    ]
    keep_attachments = _airtable_attachment_filenames(keep_fields.get("Attachments"))
    duplicate_attachments = _airtable_attachment_filenames(duplicate_fields.get("Attachments"))
    if keep_attachments != duplicate_attachments or not keep_attachments:
        mismatched_identity_fields.append("Attachments")
    if mismatched_identity_fields:
        return {
            "status": "blocked",
            "operation": "reconcile_duplicate_expense",
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "reason": "The two records were not verified as exact expense duplicates.",
            "mismatched_identity_fields": sorted(set(mismatched_identity_fields)),
            "send_enabled": False,
        }

    update_result = airtable_write_record_impl(
        json.dumps({"Estimated Tax Periods": period}),
        table=table_name,
        base_alias=base_alias,
        base_id=config["base_id"],
        record_id=keep_id,
        approval_reference=approval_reference,
        operation="update",
        validate_schema=True,
        live=True,
    )
    update_verification = update_result.get("verification", {})
    if update_result.get("status") != "success" or not (
        isinstance(update_verification, dict) and update_verification.get("passed") is True
    ):
        return {
            "status": "verification_failed",
            "operation": "reconcile_duplicate_expense",
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "reason": "The original expense could not be verified after correction.",
            "update_result": update_result,
            "send_enabled": False,
        }
    corrected_records = read_exact(keep_id)
    corrected_fields = corrected_records[0].get("fields", {}) if corrected_records else {}
    corrected_attachments = _airtable_attachment_filenames(
        corrected_fields.get("Attachments") if isinstance(corrected_fields, dict) else None
    )
    if corrected_attachments != keep_attachments:
        return {
            "status": "verification_failed",
            "operation": "reconcile_duplicate_expense",
            "record_id": keep_id,
            "duplicate_record_id": duplicate_id,
            "reason": "The original receipt attachment was not preserved after correction.",
            "send_enabled": False,
        }

    delete_request = {
        "method": "DELETE",
        "url": (
            f"{_airtable_table_url(table_name, base_id=config['base_id'])}/"
            f"{quote(duplicate_id, safe='')}"
        ),
        "table": table_name,
        "params": {},
    }
    delete_payload = _airtable_send(delete_request, access_token=config["access_token"])
    duplicate_absent = not read_exact(duplicate_id)
    provider_deleted = (
        bool(delete_payload.get("deleted")) if isinstance(delete_payload, dict) else False
    )
    passed = provider_deleted and duplicate_absent
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "reconcile_duplicate_expense",
        "table": table_name,
        "record_id": keep_id,
        "duplicate_record_id": duplicate_id,
        "target_estimated_tax_period": period,
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "record_id_match": True,
            "updated_period": corrected_fields.get("Estimated Tax Periods") == period,
            "attachment_read_back": corrected_attachments == keep_attachments,
            "attachment_filenames": keep_attachments,
            "duplicate_provider_deleted": provider_deleted,
            "duplicate_record_absent_after": duplicate_absent,
        },
        "send_enabled": False,
        "audit_notes": [
            "Provider reads proved the two exact records represented the same expense.",
            "The original record was corrected and its attachment verified before duplicate deletion.",
            "Provider read-back checked duplicate absence after deletion.",
        ],
    }


def _airtable_attachment_filenames(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        str(item.get("filename") or "").strip()
        for item in value
        if isinstance(item, dict) and str(item.get("filename") or "").strip()
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def airtable_reconcile_duplicate_expense(
    keep_record_id: str,
    duplicate_record_id: str,
    target_estimated_tax_period: str,
    table: str = "Personal Expenses",
    base_alias: str = "finance_tax_tracker",
    base_id: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Correct one exact expense and remove one exact provider-verified duplicate."""

    return json.dumps(
        airtable_reconcile_duplicate_expense_impl(
            keep_record_id,
            duplicate_record_id,
            target_estimated_tax_period=target_estimated_tax_period,
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
        raise ValueError("The bounded Airtable test lifecycle supports only Business Expenses.")
    clean_approval = str(
        approval_reference or os.getenv(AIRTABLE_OPERATOR_APPROVAL_ENV, "")
    ).strip()
    if live and not clean_approval:
        raise RuntimeError("The Airtable test lifecycle requires a non-empty approval_reference.")
    if live and not parse_bool(os.getenv("AIRTABLE_ALLOW_TEST_DELETES")):
        raise RuntimeError(
            "The Airtable test lifecycle requires its cleanup gate before create. "
            "Set AIRTABLE_ALLOW_TEST_DELETES=true for the approved lifecycle window."
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
        "provider_link": (
            f"https://airtable.com/{quote(_airtable_base_config(base_alias=base_alias)['base_id'], safe='')}"
            if passed
            else ""
        ),
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
            "provider_link",
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


@durable_provider_tool("airtable_upload_attachment")
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
    record_provider_observation(
        {
            "status": "observed",
            "provider": "airtable",
            "operation": "upload_attachment",
            "record_id": clean_record_id,
            "provider_write": True,
            "verification": {"passed": False, "status": "pending_readback"},
        }
    )
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
    matching = (
        [
            item
            for item in after_attachments
            if isinstance(item, Mapping)
            and str(item.get("filename") or "") == local_file.filename
            and int(item.get("size") or 0) == local_file.size_bytes
        ]
        if isinstance(after_attachments, list)
        else []
    )
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


@durable_provider_tool("airtable_link_attachment")
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
            "Airtable attachment links are disabled. Set AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true."
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
    record_provider_observation(
        {
            "status": "observed",
            "provider": "airtable",
            "operation": "link_attachment",
            "record_id": clean_record_id,
            "provider_write": True,
            "verification": {"passed": False, "status": "pending_readback"},
        }
    )
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


@durable_provider_tool("airtable_create_expense_from_receipt")
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

    approval_reference = (
        approval_reference.strip() or os.getenv(AIRTABLE_OPERATOR_APPROVAL_ENV, "").strip()
    )

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
    evidence_period = _airtable_estimated_period_number(evidence.estimated_tax_periods)
    requested_period = _airtable_estimated_period_number(fields.get("Estimated Tax Periods"))
    if fields.get("Estimated Tax Periods") not in (None, "") and requested_period is None:
        return {
            "status": "blocked",
            "operation": "create_expense_from_receipt",
            "reason": "Estimated Tax Periods must resolve to one of 1, 2, 3, or 4.",
            "table": table_name,
            "send_enabled": False,
        }
    if evidence_period is not None and requested_period not in (None, evidence_period):
        return {
            "status": "blocked",
            "operation": "create_expense_from_receipt",
            "reason": "The requested tax period conflicts with the receipt date.",
            "table": table_name,
            "receipt_period": str(evidence_period),
            "requested_period": str(requested_period),
            "send_enabled": False,
        }
    canonical_period = evidence_period or requested_period
    canonical_period_field_present = "Estimated Tax Periods" in field_by_name
    if canonical_period is not None and canonical_period_field_present:
        fields["Estimated Tax Periods"] = str(canonical_period)
    if not fields:
        return {
            "status": "blocked",
            "reason": "No receipt-backed schema fields were available for the expense create.",
            "mapping": mapping,
            "send_enabled": False,
        }
    attachment_field = mapping.get("attachment_field") if isinstance(mapping, Mapping) else {}
    if not isinstance(attachment_field, Mapping) or not attachment_field:
        return {
            "status": "blocked",
            "operation": "create_expense_from_receipt",
            "reason": (
                "The target expense table does not expose a multipleAttachments field; "
                "no Airtable record was created."
            ),
            "table": table_name,
            "mapping": mapping,
            "approval_reference": approval_reference.strip(),
            "verification": {
                "status": "blocked_before_write",
                "passed": False,
                "create_read_back": False,
                "attachment_read_back": False,
            },
            "send_enabled": False,
        }
    live_write_requested = live and not parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    period_context: dict[str, Any] = {
        "status": "not_required",
        "canonical_value": (
            str(canonical_period)
            if canonical_period is not None and canonical_period_field_present
            else ""
        ),
        "provider_read": False,
    }
    if live_write_requested:
        if not approval_reference.strip():
            raise RuntimeError("Airtable receipt expense creates require approval_reference.")
        if not parse_bool(os.getenv("AIRTABLE_ALLOW_WRITES")):
            raise RuntimeError("Airtable live writes are disabled. Set AIRTABLE_ALLOW_WRITES=true.")
        if (
            isinstance(attachment_field, Mapping)
            and attachment_field
            and not parse_bool(os.getenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS"))
        ):
            raise RuntimeError(
                "Airtable attachment uploads are disabled. Set "
                "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true before creating receipt-backed expenses."
            )
        _require_airtable_credentials(
            base_id=config["base_id"], access_token=config["access_token"]
        )
        if canonical_period is not None and canonical_period_field_present:
            period_context = _airtable_expense_period_context(
                table_name,
                base_alias=config["base_alias"] or base_alias,
                base_id=config["base_id"],
                canonical_period=str(canonical_period),
            )
            if period_context.get("status") != "verified":
                return {
                    "status": "blocked",
                    "operation": "create_expense_from_receipt",
                    "reason": str(
                        period_context.get("reason")
                        or "The existing Airtable tax-period values could not be verified."
                    ),
                    "table": table_name,
                    "period_context": period_context,
                    "send_enabled": False,
                }
    write_result = airtable_write_record_impl(
        json.dumps(fields, ensure_ascii=True, sort_keys=True),
        table=table_name,
        base_alias=config["base_alias"] or base_alias,
        base_id=config["base_id"],
        approval_reference=approval_reference,
        operation="create",
        validate_schema=True,
        live=live,
    )
    record_id = str(write_result.get("record_id") or "").strip()
    dry_run = write_result.get("status") == "dry-run"
    write_verification = (
        write_result.get("verification")
        if isinstance(write_result.get("verification"), Mapping)
        else {}
    )
    record_verified = bool(
        write_result.get("status") == "success" and write_verification.get("passed") is True
    )
    attachment_result: dict[str, Any] = {}
    if dry_run or record_verified:
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
    attachment_verification = (
        attachment_result.get("verification")
        if isinstance(attachment_result.get("verification"), Mapping)
        else {}
    )
    attachment_verified = bool(
        attachment_result.get("status") == "success"
        and attachment_verification.get("passed") is True
    )
    status = (
        "success"
        if record_verified and attachment_verified
        else "dry-run"
        if dry_run
        else "partial"
    )
    return {
        "status": status,
        "operation": "create_expense_from_receipt",
        "table": table_name,
        "record_id": record_id,
        "provider_link": (
            str(write_result.get("provider_link") or "") if status == "success" else ""
        ),
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
        "period_context": period_context,
        "write_result": write_result,
        "attachment_result": attachment_result,
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "verified" if status == "success" else "preview" if dry_run else "partial",
            "passed": status == "success",
            "record_id_match": bool(
                record_id and write_verification.get("record_id_match") is True
            ),
            "create_read_back": record_verified,
            "attachment_read_back": attachment_verified,
            "attachment_count_before": attachment_verification.get("attachment_count_before"),
            "attachment_count_after": attachment_verification.get("attachment_count_after"),
        },
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


def _google_doc_semantic_window(
    annotations: list[dict[str, Any]],
    *,
    start: int,
) -> tuple[list[dict[str, Any]], int]:
    output: list[dict[str, Any]] = []
    used_chars = 0
    index = min(start, len(annotations))
    while index < len(annotations):
        annotation = annotations[index]
        serialized_length = len(
            json.dumps(annotation, ensure_ascii=True, sort_keys=True, default=str)
        )
        if output and used_chars + serialized_length > GOOGLE_DOC_SEMANTIC_ANNOTATION_MAX_CHARS:
            break
        if serialized_length > GOOGLE_DOC_SEMANTIC_ANNOTATION_MAX_CHARS:
            break
        output.append(annotation)
        used_chars += serialized_length
        index += 1
    return output, index


def google_doc_read_impl(
    document_id_or_url: str,
    *,
    folder_path: str = "",
    max_chars: int = 6000,
    start_char: int = 0,
    semantic_start: int = 0,
    expected_revision_id: str = "",
    expected_snapshot_sha256: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Read a Google Doc body through the configured OAuth token when live-enabled."""

    document_id = _google_doc_id(document_id_or_url)
    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_chars = min(max(int(max_chars or 6000), 1000), 12000)
    bounded_start = int(start_char or 0)
    if bounded_start < 0 or bounded_start > 10_000_000:
        raise ValueError("start_char must be between 0 and 10000000.")
    bounded_semantic_start = int(semantic_start or 0)
    if bounded_semantic_start < 0 or bounded_semantic_start > GOOGLE_DOC_MAX_STRUCTURE_NODES:
        raise ValueError(f"semantic_start must be between 0 and {GOOGLE_DOC_MAX_STRUCTURE_NODES}.")
    expected_revision = str(expected_revision_id or "").strip()
    if len(expected_revision) > 500:
        raise ValueError("expected_revision_id must be at most 500 characters.")
    expected_snapshot = str(expected_snapshot_sha256 or "").strip().lower()
    if expected_snapshot and not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot):
        raise ValueError("expected_snapshot_sha256 must be a 64-character SHA-256 hex digest.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_doc",
            "document_id": document_id,
            "folder_path": target_folder_path,
            "max_chars": bounded_chars,
            "start_char": bounded_start,
            "semantic_start": bounded_semantic_start,
            "expected_revision_id": expected_revision,
            "expected_snapshot_sha256": expected_snapshot,
            "send_enabled": False,
        }
    _start_google_workspace_read_attempt()
    services = _google_workspace_services()
    _assert_configured_google_account(services["drive"])
    _assert_drive_file_in_folder(services["drive"], document_id, target_folder_path)
    docs_service = services["docs"]
    document = (
        docs_service.documents()
        .get(
            documentId=document_id,
            includeTabsContent=True,
            suggestionsViewMode="SUGGESTIONS_INLINE",
        )
        .execute()
    )
    title = str(document.get("title", ""))
    revision_id = str(document.get("revisionId") or "")
    source_version = {
        "revision_id": revision_id,
        "status": "available" if revision_id else "unavailable",
    }
    common = {
        "operation": "read_doc",
        "document_id": document_id,
        "source_version": source_version,
        "revision_check": {
            "expected_revision_id": expected_revision,
            "matches_expected": (revision_id == expected_revision if expected_revision else None),
        },
        "provider_link": f"https://docs.google.com/document/d/{document_id}/edit",
        "title": title,
        "send_enabled": False,
    }
    if expected_revision and revision_id and revision_id != expected_revision:
        return {
            **common,
            "status": "source_changed",
            "reason_code": "source_revision_changed",
            "text": "",
            "limitations": [
                "The Google Doc revision changed before this bounded continuation; no "
                "content from the new revision was mixed with the earlier read."
            ],
        }
    if (bounded_start or bounded_semantic_start) and not (expected_revision or expected_snapshot):
        return {
            **common,
            "status": "continuation_identity_required",
            "reason_code": "source_continuation_identity_required",
            "text": "",
            "limitations": [
                "A nonzero text or semantic cursor requires the exact revision ID or "
                "snapshot digest returned by the initial read."
            ],
        }
    extraction = _google_doc_extraction(document)
    full_text = str(extraction["text"])
    snapshot_sha256 = hashlib.sha256(
        json.dumps(
            {
                "document_id": document_id,
                "suggestions_view_mode": extraction["semantic_coverage"].get(
                    "suggestions_view_mode"
                ),
                "text": full_text,
                "semantic_annotations": extraction["semantic_annotations"],
                "structure": extraction["structure"],
            },
            ensure_ascii=True,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    common["source_snapshot"] = {
        "sha256": snapshot_sha256,
        "basis": "document_id+suggestions_view+extracted_text+semantics+structure",
        "expected_sha256": expected_snapshot,
        "matches_expected": (snapshot_sha256 == expected_snapshot if expected_snapshot else None),
    }
    if expected_snapshot and snapshot_sha256 != expected_snapshot:
        return {
            **common,
            "status": "source_changed",
            "reason_code": "source_snapshot_changed",
            "text": "",
            "limitations": [
                "The canonical Google Doc extraction changed before this bounded "
                "continuation; no content from the changed snapshot was mixed with the "
                "earlier read."
            ],
        }
    if (
        (bounded_start or bounded_semantic_start)
        and expected_revision
        and not revision_id
        and not expected_snapshot
    ):
        return {
            **common,
            "status": "continuation_unavailable",
            "reason_code": "source_revision_unavailable",
            "text": "",
            "limitations": [
                "The provider stopped returning a revision ID and no expected snapshot "
                "digest was supplied, so continuation cannot safely proceed."
            ],
        }
    window_start = min(bounded_start, len(full_text))
    window_end = min(len(full_text), window_start + bounded_chars)
    text = full_text[window_start:window_end]
    has_more = window_end < len(full_text)
    windowed = window_start > 0
    all_semantic_annotations = list(extraction["semantic_annotations"])
    semantic_annotations, semantic_end = _google_doc_semantic_window(
        all_semantic_annotations,
        start=bounded_semantic_start,
    )
    semantic_has_more = semantic_end < len(all_semantic_annotations)
    semantic_windowed = bounded_semantic_start > 0
    truncated = has_more or windowed or semantic_has_more or semantic_windowed
    limitations = list(extraction["limitations"])
    if has_more:
        limitations.append(
            f"Extracted text exceeded this {bounded_chars}-character response window."
        )
    if windowed:
        limitations.append(
            "This response is a bounded continuation and does not repeat earlier text."
        )
    if semantic_has_more:
        limitations.append(
            "Additional semantic annotations exist beyond this bounded semantic window."
        )
    if semantic_windowed:
        limitations.append(
            "This response is a bounded semantic continuation and does not repeat earlier "
            "semantic annotations."
        )
    if has_more and not revision_id:
        limitations.append(
            "The provider did not return a revision ID; continuation is protected by the "
            "canonical source snapshot digest instead."
        )
    content_complete = bool(extraction["content_complete"]) and not truncated
    supported_text_complete = bool(extraction["supported_text_complete"]) and not truncated
    if truncated and not extraction["content_complete"]:
        extraction_status = "partial_window"
    elif has_more or semantic_has_more:
        extraction_status = "truncated"
    elif windowed or semantic_windowed:
        extraction_status = "windowed"
    elif not extraction["content_complete"]:
        extraction_status = "partial"
    else:
        extraction_status = "complete"
    continuation_available = has_more or semantic_has_more
    next_request = (
        {
            "start_char": window_end,
            "semantic_start": semantic_end,
            "expected_revision_id": revision_id,
            "expected_snapshot_sha256": snapshot_sha256,
            "max_chars": bounded_chars,
        }
        if continuation_available
        else None
    )
    return {
        **common,
        "status": "success",
        "text": text,
        "char_count": len(text),
        "extracted_char_count": len(full_text),
        "truncated": truncated,
        "extraction_status": extraction_status,
        "content_complete": content_complete,
        "supported_text_complete": supported_text_complete,
        "visual_content_status": extraction["visual_content_status"],
        "semantic_annotations": semantic_annotations,
        "semantic_annotations_scope": "bounded_semantic_window",
        "semantic_coverage": {
            **extraction["semantic_coverage"],
            "annotations_truncated": (
                extraction["semantic_coverage"]["annotations_truncated"] or semantic_has_more
            ),
            "document_annotation_count": len(all_semantic_annotations),
            "returned_annotation_count": len(semantic_annotations),
        },
        "structure": extraction["structure"],
        "limitations": limitations,
        "read_window": {
            "unit": "extracted_unicode_characters",
            "start_char": window_start,
            "end_char": window_end,
            "full_char_count": len(full_text),
            "has_more": has_more,
            "window_complete": not has_more,
            "part_id": (
                f"google-doc:{document_id}:"
                f"{f'revision:{revision_id}' if revision_id else f'snapshot:{snapshot_sha256}'}:"
                f"chars:{window_start}-{window_end}"
            ),
        },
        "semantic_window": {
            "unit": "semantic_annotation_index",
            "start": min(bounded_semantic_start, len(all_semantic_annotations)),
            "end": semantic_end,
            "full_count": len(all_semantic_annotations),
            "has_more": semantic_has_more,
            "window_complete": not semantic_has_more,
            "part_id": (
                f"google-doc:{document_id}:"
                f"{f'revision:{revision_id}' if revision_id else f'snapshot:{snapshot_sha256}'}:"
                f"semantics:{min(bounded_semantic_start, len(all_semantic_annotations))}-"
                f"{semantic_end}"
            ),
        },
        "continuation": {
            "supported": True,
            "available": continuation_available,
            "next_request": next_request,
            "revision_or_snapshot_required": True,
        },
        "extraction_annotations": {
            "format": "double_bracket_structure_markers_plus_structured_semantics",
            "source_content": False,
            "purpose": (
                "Preserve structural relationships and identify unread or unsupported "
                "content without treating source marker-like text as semantic metadata."
            ),
        },
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_read(
    document_id_or_url: str,
    folder_path: str = "",
    max_chars: int = 6000,
    start_char: Annotated[int, Field(ge=0, le=10_000_000)] = 0,
    semantic_start: Annotated[int, Field(ge=0, le=GOOGLE_DOC_MAX_STRUCTURE_NODES)] = 0,
    expected_revision_id: Annotated[str, Field(max_length=500)] = "",
    expected_snapshot_sha256: Annotated[str, Field(max_length=64)] = "",
    live: bool = False,
) -> str:
    """Read one bounded, revision-safe window of an approved Google Doc."""

    result = _google_workspace_read_call(
        "google_doc_read",
        "read_doc",
        lambda: google_doc_read_impl(
            document_id_or_url,
            folder_path=folder_path,
            max_chars=max_chars,
            start_char=start_char,
            semantic_start=semantic_start,
            expected_revision_id=expected_revision_id,
            expected_snapshot_sha256=expected_snapshot_sha256,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
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
        raise RuntimeError("The exact Workspace target is not a Google Slides or PowerPoint deck.")

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

    result = _google_workspace_read_call(
        "google_slide_deck_read",
        "read_slide_deck",
        lambda: google_slide_deck_read_impl(
            presentation_id_or_url,
            folder_path=folder_path,
            max_slides=max_slides,
            max_chars_per_slide=max_chars_per_slide,
            include_speaker_notes=include_speaker_notes,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_slide_deck_write_impl(
    title: str,
    slides: Sequence[Mapping[str, str]],
    *,
    presentation_id_or_url: str = "",
    content_mode: Literal["replace", "append"] = "replace",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create or edit one scoped Google Slides deck with verified title/body slides."""

    cleaned_title = " ".join(str(title or "Keystone Presentation").split())[:300]
    target_id = _google_drive_file_id(presentation_id_or_url)
    normalized_mode = str(content_mode or "replace").strip().lower()
    if normalized_mode not in {"replace", "append"}:
        raise ValueError("content_mode must be replace or append.")
    normalized_slides = _normalize_google_slide_inputs(slides)
    if not normalized_slides:
        raise ValueError("At least one slide with a title or body is required.")
    target_folder_path = _google_docs_folder_path(folder_path)
    if not live:
        return {
            "status": "dry-run",
            "operation": "write_slide_deck",
            "presentation_id": target_id,
            "title": cleaned_title,
            "content_mode": normalized_mode,
            "folder_path": target_folder_path,
            "slide_count": len(normalized_slides),
            "slides_preview": normalized_slides,
            "approval_reference": approval_reference.strip(),
            "provider_mutated": False,
            "send_enabled": False,
        }

    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    slides_service = services["slides"]
    _assert_configured_google_account(drive_service)
    created = not bool(target_id)
    existing_slides: list[dict[str, Any]] = []
    existing_slide_ids: list[str] = []
    inserted_slide_ids: list[str] = []
    original_title = ""
    title_updated = False
    staged_content_verified = False
    staging_mutation_applied = False
    old_slides_deleted = False
    final_slide_content_verified = False
    old_slides_absent_verified = normalized_mode != "replace"
    drive_identity_verified = False
    drive_title_verified = False
    final_verification_blocker = ""
    initial_revision_id = ""
    staged_revision_id = ""
    try:
        if created:
            folder_id = _find_drive_folder_path(drive_service, target_folder_path)
            if not folder_id:
                raise RuntimeError(
                    "The approved Google Slides destination folder does not exist. "
                    "Create the folder through its separately approved operation first."
                )
            created_payload = (
                slides_service.presentations().create(body={"title": cleaned_title}).execute()
            )
            if not isinstance(created_payload, dict):
                raise RuntimeError("Google Slides create returned an unexpected payload.")
            target_id = str(created_payload.get("presentationId") or "")
            if not target_id:
                raise RuntimeError("Google Slides create returned no presentation ID.")
            if folder_id:
                _move_drive_file_to_folder(drive_service, target_id, folder_id)
        else:
            _assert_drive_file_in_folder(drive_service, target_id, target_folder_path)
            metadata = (
                drive_service.files()
                .get(fileId=target_id, fields="id,name,mimeType,trashed,parents")
                .execute()
            )
            if (
                not isinstance(metadata, dict)
                or metadata.get("mimeType") != GOOGLE_SLIDES_MIME_TYPE
            ):
                raise RuntimeError("The exact Workspace target is not a Google Slides deck.")
            original_title = str(metadata.get("name") or "")
            if original_title != cleaned_title:
                title_payload = (
                    drive_service.files()
                    .update(
                        fileId=target_id,
                        body={"name": cleaned_title},
                        fields="id,name",
                    )
                    .execute()
                )
                if (
                    not isinstance(title_payload, dict)
                    or str(title_payload.get("id") or "") != target_id
                    or str(title_payload.get("name") or "") != cleaned_title
                ):
                    raise RuntimeError(
                        "Google Drive did not verify the requested presentation title update."
                    )
                title_updated = True

        existing = slides_service.presentations().get(presentationId=target_id).execute()
        initial_revision_id = (
            str(existing.get("revisionId") or "") if isinstance(existing, dict) else ""
        )
        if not initial_revision_id:
            raise RuntimeError(
                "Google Slides did not return a revision ID for concurrency-safe editing."
            )
        existing_slides = (
            [slide for slide in existing.get("slides", []) if isinstance(slide, dict)]
            if isinstance(existing, dict)
            else []
        )
        existing_slide_ids = [
            str(slide.get("objectId") or "")
            for slide in existing_slides
            if str(slide.get("objectId") or "")
        ]
        requests, inserted_slide_ids = _google_slide_write_requests(
            normalized_slides,
            insertion_index=len(existing_slides) if normalized_mode == "append" else 0,
        )
        staged_response = (
            slides_service.presentations()
            .batchUpdate(
                presentationId=target_id,
                body={
                    "requests": requests,
                    "writeControl": {"requiredRevisionId": initial_revision_id},
                },
            )
            .execute()
        )
        staging_mutation_applied = True
        staged = slides_service.presentations().get(presentationId=target_id).execute()
        staged_revision_id = (
            str(staged.get("revisionId") or "") if isinstance(staged, dict) else ""
        ) or (
            str((staged_response.get("writeControl") or {}).get("requiredRevisionId") or "")
            if isinstance(staged_response, dict)
            and isinstance(staged_response.get("writeControl"), Mapping)
            else ""
        )
        artifacts = _google_slides_artifacts(
            staged,
            presentation_id=target_id,
            max_slides=100,
            max_chars=12_000,
            include_speaker_notes=False,
        )
        artifact_by_id = {
            str(slide.get("slide_id") or ""): slide
            for slide in artifacts["all_slides"]
            if isinstance(slide, dict)
        }
        inserted = [artifact_by_id.get(slide_id, {}) for slide_id in inserted_slide_ids]
        content_verified = all(
            expected["title"] in str(actual.get("text") or "")
            and expected["body"] in str(actual.get("text") or "")
            for expected, actual in zip(normalized_slides, inserted, strict=True)
        )
        if not content_verified:
            raise RuntimeError(
                "Google Slides provider read-back did not verify the requested slide content."
            )
        staged_content_verified = True

        if normalized_mode == "replace" and existing_slide_ids:
            if not staged_revision_id:
                raise RuntimeError(
                    "Google Slides did not return the staged revision ID required before "
                    "replacing existing slides."
                )
            slides_service.presentations().batchUpdate(
                presentationId=target_id,
                body={
                    "requests": [
                        {"deleteObject": {"objectId": slide_id}} for slide_id in existing_slide_ids
                    ],
                    "writeControl": {"requiredRevisionId": staged_revision_id},
                },
            ).execute()
            old_slides_deleted = True

        try:
            final_payload = slides_service.presentations().get(presentationId=target_id).execute()
            final_artifacts = _google_slides_artifacts(
                final_payload,
                presentation_id=target_id,
                max_slides=100,
                max_chars=12_000,
                include_speaker_notes=False,
            )
            final_by_id = {
                str(slide.get("slide_id") or ""): slide
                for slide in final_artifacts["all_slides"]
                if isinstance(slide, dict)
            }
            final_inserted = [final_by_id.get(slide_id, {}) for slide_id in inserted_slide_ids]
            final_slide_content_verified = all(
                expected["title"] in str(actual.get("text") or "")
                and expected["body"] in str(actual.get("text") or "")
                for expected, actual in zip(normalized_slides, final_inserted, strict=True)
            )
            old_slides_absent_verified = normalized_mode != "replace" or all(
                slide_id not in final_by_id for slide_id in existing_slide_ids
            )
            if not final_slide_content_verified or not old_slides_absent_verified:
                final_verification_blocker = (
                    "Final provider read-back did not fully verify the staged content "
                    "and replacement boundary."
                )
        except Exception as exc:
            if old_slides_deleted or normalized_mode == "append":
                final_verification_blocker = (
                    "The new slides were verified before the final mutation, but final "
                    f"read-back is pending: {type(exc).__name__}: {exc}"
                )[:700]
            else:
                raise

        try:
            final_metadata = (
                drive_service.files()
                .get(fileId=target_id, fields="id,name,mimeType,trashed")
                .execute()
            )
            if (
                isinstance(final_metadata, dict)
                and str(final_metadata.get("id") or "") == target_id
                and str(final_metadata.get("mimeType") or "") == GOOGLE_SLIDES_MIME_TYPE
                and not bool(final_metadata.get("trashed"))
            ):
                drive_identity_verified = True
            if drive_identity_verified and str(final_metadata.get("name") or "") == cleaned_title:
                drive_title_verified = True
            if not drive_identity_verified or not drive_title_verified:
                final_verification_blocker = (
                    "Google Drive read-back did not fully verify the presentation identity, "
                    "title, type, and active state."
                )
        except Exception as exc:
            final_verification_blocker = (
                "Slide content was staged and verified, but final Drive identity/title "
                f"read-back is pending: {type(exc).__name__}: {exc}"
            )[:700]
    except Exception as exc:
        compensation_errors: list[str] = []
        if created and target_id:
            try:
                drive_service.files().update(
                    fileId=target_id,
                    body={"trashed": True},
                    fields="id,trashed",
                ).execute()
            except Exception as compensation_exc:
                compensation_errors.append(
                    "new-deck trash compensation failed: "
                    f"{type(compensation_exc).__name__}: {compensation_exc}"
                )
        elif target_id:
            if staging_mutation_applied and inserted_slide_ids and not old_slides_deleted:
                try:
                    cleanup_body: dict[str, Any] = {
                        "requests": [
                            {"deleteObject": {"objectId": slide_id}}
                            for slide_id in inserted_slide_ids
                        ]
                    }
                    if staged_revision_id:
                        cleanup_body["writeControl"] = {"requiredRevisionId": staged_revision_id}
                    slides_service.presentations().batchUpdate(
                        presentationId=target_id,
                        body=cleanup_body,
                    ).execute()
                except Exception as compensation_exc:
                    compensation_errors.append(
                        "staged-slide cleanup failed: "
                        f"{type(compensation_exc).__name__}: {compensation_exc}"
                    )
            if title_updated:
                try:
                    restored = (
                        drive_service.files()
                        .update(
                            fileId=target_id,
                            body={"name": original_title},
                            fields="id,name",
                        )
                        .execute()
                    )
                    if (
                        not isinstance(restored, dict)
                        or str(restored.get("name") or "") != original_title
                    ):
                        raise RuntimeError("provider did not verify the restored title")
                except Exception as compensation_exc:
                    compensation_errors.append(
                        "title restoration failed: "
                        f"{type(compensation_exc).__name__}: {compensation_exc}"
                    )
        if compensation_errors:
            raise RuntimeError(
                f"{type(exc).__name__}: {exc}. Compensation incomplete for "
                f"presentation {target_id}: {'; '.join(compensation_errors)}"
            ) from exc
        raise

    return {
        "status": "partial" if final_verification_blocker else "success",
        "operation": "write_slide_deck",
        "presentation_id": target_id,
        "title": cleaned_title,
        "content_mode": normalized_mode,
        "created": created,
        "inserted_slide_ids": inserted_slide_ids,
        "inserted_slide_count": len(inserted_slide_ids),
        "replaced_slide_count": (
            len(existing_slide_ids) if normalized_mode == "replace" and old_slides_deleted else 0
        ),
        "folder_path": target_folder_path,
        "provider_link": f"https://docs.google.com/presentation/d/{target_id}/edit",
        "approval_reference": approval_reference.strip(),
        "verification": {
            "status": "pending_readback" if final_verification_blocker else "verified",
            "passed": not bool(final_verification_blocker),
            "presentation_id_match": drive_identity_verified,
            "title_match": drive_title_verified,
            "slide_content_staged_match": staged_content_verified,
            "slide_content_match": final_slide_content_verified,
            "replaced_slides_absent": old_slides_absent_verified,
            "optimistic_concurrency_used": bool(initial_revision_id),
            "blocker": final_verification_blocker,
        },
        "provider_mutated": True,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_slide_deck_write(
    title: Annotated[str, Field(min_length=1, max_length=300)],
    slides_json: Annotated[str, Field(min_length=2, max_length=260_000)],
    presentation_id_or_url: Annotated[str, Field(max_length=2_000)] = "",
    content_mode: Literal["replace", "append"] = "replace",
    folder_path: Annotated[str, Field(max_length=500)] = "",
    approval_reference: Annotated[str, Field(max_length=300)] = "",
    live: bool = False,
) -> str:
    """Create or edit an approved Google Slides deck and verify provider content."""

    try:
        parsed_slides = json.loads(slides_json)
    except json.JSONDecodeError as exc:
        raise ValueError("slides_json must be a JSON array of title/body objects.") from exc
    if not isinstance(parsed_slides, list):
        raise ValueError("slides_json must be a JSON array of title/body objects.")
    return json.dumps(
        google_slide_deck_write_impl(
            title,
            parsed_slides,
            presentation_id_or_url=presentation_id_or_url,
            content_mode=content_mode,
            folder_path=folder_path,
            approval_reference=approval_reference,
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def _normalize_google_slide_inputs(
    slides: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in list(slides)[:30]:
        if not isinstance(raw, Mapping):
            raise ValueError("Each slide must be an object with title and body fields.")
        title = " ".join(str(raw.get("title") or "").split())[:500]
        body = "\n".join(line.rstrip() for line in str(raw.get("body") or "").splitlines()).strip()[
            :8_000
        ]
        if title or body:
            normalized.append({"title": title, "body": body})
    return normalized


def _google_slide_write_requests(
    slides: Sequence[Mapping[str, str]],
    *,
    insertion_index: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    requests: list[dict[str, Any]] = []
    slide_ids: list[str] = []
    for offset, slide in enumerate(slides):
        suffix = uuid4().hex[:16]
        slide_id = f"kba_slide_{suffix}"
        title_id = f"kba_title_{suffix}"
        body_id = f"kba_body_{suffix}"
        slide_ids.append(slide_id)
        requests.append(
            {
                "createSlide": {
                    "objectId": slide_id,
                    "insertionIndex": insertion_index + offset,
                    "slideLayoutReference": {"predefinedLayout": "BLANK"},
                }
            }
        )
        title = str(slide.get("title") or "")
        if title:
            requests.extend(
                [
                    _google_slide_text_box_request(
                        slide_id=slide_id,
                        object_id=title_id,
                        width=640,
                        height=70,
                        x=40,
                        y=28,
                    ),
                    {
                        "insertText": {
                            "objectId": title_id,
                            "text": title,
                            "insertionIndex": 0,
                        }
                    },
                ]
            )
        body = str(slide.get("body") or "")
        if body:
            requests.extend(
                [
                    _google_slide_text_box_request(
                        slide_id=slide_id,
                        object_id=body_id,
                        width=640,
                        height=390,
                        x=40,
                        y=115,
                    ),
                    {
                        "insertText": {
                            "objectId": body_id,
                            "text": body,
                            "insertionIndex": 0,
                        }
                    },
                ]
            )
    return requests, slide_ids


def _google_slide_text_box_request(
    *,
    slide_id: str,
    object_id: str,
    width: int,
    height: int,
    x: int,
    y: int,
) -> dict[str, Any]:
    return {
        "createShape": {
            "objectId": object_id,
            "shapeType": "TEXT_BOX",
            "elementProperties": {
                "pageObjectId": slide_id,
                "size": {
                    "width": {"magnitude": width, "unit": "PT"},
                    "height": {"magnitude": height, "unit": "PT"},
                },
                "transform": {
                    "scaleX": 1,
                    "scaleY": 1,
                    "translateX": x,
                    "translateY": y,
                    "unit": "PT",
                },
            },
        }
    }


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
                "modified_time": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
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
        raise RuntimeError(f"slide_number {page} exceeds the deck's {slide_count} slides.")
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


@durable_provider_tool("google_doc_write")
def google_doc_write_impl(
    title: str,
    body_text: str,
    *,
    document_id: str = "",
    content_mode: Literal["replace", "append"] = "replace",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create, replace, or append a Google Doc behind approval and env gates."""

    cleaned_title = " ".join(str(title or "Keystone Chief of Staff Artifact").split())
    body = str(body_text or "").strip()
    target_folder_path = _google_docs_folder_path(folder_path)
    normalized_mode = str(content_mode or "replace").strip().lower()
    if normalized_mode not in {"replace", "append"}:
        raise ValueError("content_mode must be replace or append.")
    if not body:
        raise ValueError("body_text is required for Google Doc writes.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "write_doc",
            "title": cleaned_title,
            "document_id": document_id.strip(),
            "content_mode": normalized_mode,
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
        requests = (
            _append_doc_requests(docs_service, target_id, body)
            if normalized_mode == "append"
            else _replace_doc_requests(docs_service, target_id, body)
        )
        docs_service.documents().batchUpdate(
            documentId=target_id,
            body={"requests": requests},
        ).execute()
        record_provider_observation(
            {
                "status": "observed",
                "provider": "google_workspace",
                "operation": "write_doc",
                "document_id": target_id,
                "provider_write": True,
                "verification": {"passed": False, "status": "pending_readback"},
            }
        )
    else:
        created = docs_service.documents().create(body={"title": cleaned_title}).execute()
        target_id = str(created.get("documentId") or "").strip()
        if not target_id:
            raise RuntimeError("Google Doc create returned no provider document identity.")
        record_provider_observation(
            {
                "status": "observed",
                "provider": "google_workspace",
                "operation": "write_doc",
                "document_id": target_id,
                "provider_write": True,
                "verification": {"passed": False, "status": "pending_readback"},
            }
        )
        folder_id = _ensure_drive_folder_path(services["drive"], target_folder_path)
        if folder_id:
            _move_drive_file_to_folder(services["drive"], target_id, folder_id)
        docs_service.documents().batchUpdate(
            documentId=target_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]},
        ).execute()
    verified_document = docs_service.documents().get(documentId=target_id).execute()
    verified_text = _google_doc_text(verified_document)
    content_verified = (
        verified_text.rstrip().endswith(body)
        if normalized_mode == "append"
        else verified_text.strip() == body
    )
    if not content_verified:
        raise RuntimeError("Google Doc provider read-back did not verify the requested content.")
    return {
        "status": "success",
        "operation": "write_doc",
        "document_id": target_id,
        "title": cleaned_title,
        "content_mode": normalized_mode,
        "provider_verification": "passed",
        "content_verified": True,
        "verification": {
            "status": "verified",
            "passed": True,
            "document_id_match": bool(target_id),
            "content_match": True,
        },
        "url": f"https://docs.google.com/document/d/{target_id}/edit",
        "provider_link": f"https://docs.google.com/document/d/{target_id}/edit",
        "folder_path": target_folder_path,
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_write(
    title: str,
    body_text: str,
    document_id: str = "",
    content_mode: Literal["replace", "append"] = "replace",
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
            content_mode=content_mode,
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
        "provider_link": (metadata.get("webViewLink", "") if isinstance(metadata, dict) else ""),
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


def google_doc_test_lifecycle_impl(
    title: str,
    body_text: str,
    *,
    updated_body_text: str = "",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Create, optionally update, and trash one explicitly marked test Doc."""

    cleaned_title = " ".join(str(title or "").split())
    body = str(body_text or "").strip()
    updated_body = str(updated_body_text or "").strip()
    clean_approval = str(approval_reference or "").strip()
    if "KBA_TEST_DOC" not in cleaned_title.upper():
        raise ValueError("The Google Doc test lifecycle requires KBA_TEST_DOC in the title.")
    if not body:
        raise ValueError("body_text is required for the Google Doc test lifecycle.")
    if live and not clean_approval:
        raise RuntimeError("The Google Doc test lifecycle requires a non-empty approval_reference.")
    if live and not parse_bool(os.getenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE")):
        raise RuntimeError(
            "Google Workspace test lifecycle is disabled. Set "
            "KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE=true for the approved window."
        )

    create_result: dict[str, Any] = {}
    update_result: dict[str, Any] = {}
    trash_result: dict[str, Any] = {}
    document_id = ""
    failure = ""
    try:
        create_result = google_doc_write_impl(
            cleaned_title,
            body,
            folder_path=folder_path,
            approval_reference=f"{clean_approval}:create" if clean_approval else "",
            live=live,
        )
        document_id = str(create_result.get("document_id") or "").strip()
        if not live:
            return {
                "status": "dry-run",
                "operation": "test_doc_lifecycle",
                "title": cleaned_title,
                "required_marker": "KBA_TEST_DOC",
                "approval_reference": clean_approval,
                "create": _google_doc_lifecycle_step_receipt(create_result),
                "send_enabled": False,
            }
        if not document_id or create_result.get("content_verified") is not True:
            failure = "Google Doc create did not pass provider read-back verification."
        elif updated_body:
            update_result = google_doc_write_impl(
                cleaned_title,
                updated_body,
                document_id=document_id,
                content_mode="replace",
                folder_path=folder_path,
                approval_reference=f"{clean_approval}:update",
                live=True,
            )
            if update_result.get("content_verified") is not True:
                failure = "Google Doc update did not pass provider read-back verification."
    except Exception as exc:  # preserve a bounded receipt and still attempt cleanup
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and document_id:
            try:
                trash_result = google_doc_trash_impl(
                    document_id,
                    folder_path=folder_path,
                    approval_reference=f"{clean_approval}:trash",
                    live=True,
                )
            except Exception as exc:
                trash_result = {
                    "status": "failed",
                    "operation": "trash_doc",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "verification": {"passed": False},
                    "send_enabled": False,
                }

    create_passed = bool(
        create_result.get("status") == "success"
        and create_result.get("content_verified") is True
        and document_id
    )
    update_passed = bool(
        not updated_body
        or (
            update_result.get("status") == "success"
            and update_result.get("document_id") == document_id
            and update_result.get("content_verified") is True
        )
    )
    trash_verification = trash_result.get("verification")
    cleanup_passed = bool(
        isinstance(trash_verification, Mapping)
        and trash_verification.get("passed")
        and trash_result.get("trashed") is True
    )
    passed = create_passed and update_passed and cleanup_passed and not failure
    verification: dict[str, Any] = {
        "passed": passed,
        "create_read_back": create_passed,
        "document_trashed_after_cleanup": cleanup_passed,
    }
    if updated_body:
        verification["same_document_update_read_back"] = update_passed
    return {
        "status": "success" if passed else "failed",
        "operation": "test_doc_lifecycle",
        "document_id": document_id,
        "provider_link": str(create_result.get("provider_link") or create_result.get("url") or ""),
        "title": cleaned_title,
        "required_marker": "KBA_TEST_DOC",
        "approval_reference": clean_approval,
        "create": _google_doc_lifecycle_step_receipt(create_result),
        **({"update": _google_doc_lifecycle_step_receipt(update_result)} if updated_body else {}),
        "trash": _google_doc_lifecycle_step_receipt(trash_result),
        "verification": verification,
        "failure": failure,
        "send_enabled": False,
    }


def _google_doc_lifecycle_step_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "document_id",
            "title",
            "folder_path",
            "trashed",
            "verification",
            "send_enabled",
            "reason",
            "url",
            "provider_link",
        )
        if key in result
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_doc_test_lifecycle(
    title: str,
    body_text: str,
    updated_body_text: str = "",
    folder_path: str = "",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Run one approved KBA_TEST_DOC create/update/verify/trash lifecycle."""

    return json.dumps(
        google_doc_test_lifecycle_impl(
            title,
            body_text,
            updated_body_text=updated_body_text,
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
    _start_google_workspace_read_attempt()
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
        "identity_fingerprints": identity_fingerprints(
            item.get("id") for item in files if isinstance(item, Mapping) and item.get("id")
        ),
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

    result = _google_workspace_read_call(
        "google_drive_list_folder",
        "list_folder",
        lambda: google_drive_list_folder_impl(
            folder_path,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
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
    _start_google_workspace_read_attempt()
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
        "identity_fingerprints": identity_fingerprints(
            item.get("id") for item in files if isinstance(item, Mapping) and item.get("id")
        ),
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
    """Search by optional filename substring and MIME type, newest first.

    `query` is title text, not Drive query syntax. Leave it empty for a
    MIME-only search such as the newest PDF, and pass the MIME type separately.
    """

    result = _google_workspace_read_call(
        "google_drive_search_files",
        "search_files",
        lambda: google_drive_search_files_impl(
            query,
            folder_path=folder_path,
            mime_type=mime_type,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
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
                "owners.displayName",
            ],
            "notes": [
                "Returns Drive metadata only; it does not download file bytes.",
                "Image support includes width, height, and rotation metadata when Drive provides it.",
            ],
        }
    _start_google_workspace_read_attempt()
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
                "imageMediaMetadata,description,trashed,parents,owners(displayName,me)"
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

    result = _google_workspace_read_call(
        "google_drive_get_file_metadata",
        "get_file_metadata",
        lambda: google_drive_get_file_metadata_impl(
            file_id_or_url,
            folder_path=folder_path,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


def google_drive_media_ocr_read_impl(
    file_id_or_url: str,
    *,
    folder_path: str = "",
    max_bytes: int = 10_000_000,
    max_pages: int = 8,
    max_chars: int = 12_000,
    start_page: int = 1,
    start_char: int = 0,
    expected_content_sha256: str = "",
    expected_text_sha256: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Download one scoped Drive PDF/image and return bounded text or OCR evidence."""

    file_id = _google_drive_file_id(file_id_or_url)
    target_folder_path = _google_docs_folder_path(folder_path)
    bounded_bytes = min(max(int(max_bytes or 10_000_000), 1_000_000), 25_000_000)
    bounded_pages = min(max(int(max_pages or 8), 1), 20)
    bounded_chars = min(max(int(max_chars or 12_000), 500), 30_000)
    if start_page < 1 or start_char < 0:
        raise ValueError("start_page must be positive and start_char nonnegative.")
    if (start_page != 1 or start_char) and not expected_content_sha256:
        raise ValueError("Media continuation requires expected_content_sha256.")
    if start_char and not expected_text_sha256:
        raise ValueError("Text continuation requires expected_text_sha256.")
    if not file_id:
        raise ValueError("file_id_or_url is required.")
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_drive_media_text",
            "file_id": file_id,
            "folder_path": target_folder_path,
            "max_bytes": bounded_bytes,
            "max_pages": bounded_pages,
            "max_chars": bounded_chars,
            "start_page": start_page,
            "start_char": start_char,
            "supported_mime_types": ["application/pdf", "image/*"],
            "provider_bytes_downloaded": False,
            "send_enabled": False,
        }

    _start_google_workspace_read_attempt()
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    if not folder_id:
        return {
            "status": "missing",
            "operation": "read_drive_media_text",
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
                "id,name,mimeType,webViewLink,modifiedTime,size,imageMediaMetadata,trashed,parents"
            ),
            supportsAllDrives=False,
        )
        .execute()
    )
    if not isinstance(metadata, dict):
        raise RuntimeError("Google Drive returned non-object file metadata.")
    _assert_drive_file_under_folder(drive_service, file_id, folder_id)
    mime_type = str(metadata.get("mimeType") or "").lower()
    if mime_type != "application/pdf" and not mime_type.startswith("image/"):
        raise RuntimeError("The exact Drive target is not a supported PDF or image file.")
    declared_size = int(metadata.get("size") or 0)
    if declared_size and declared_size > bounded_bytes:
        raise RuntimeError(f"Drive media exceeds the bounded {bounded_bytes}-byte download limit.")
    image_metadata = metadata.get("imageMediaMetadata", {})
    if mime_type.startswith("image/") and isinstance(image_metadata, Mapping):
        width = int(image_metadata.get("width") or 0)
        height = int(image_metadata.get("height") or 0)
        if width > 0 and height > 0 and width * height > GOOGLE_DRIVE_OCR_MAX_PIXELS:
            raise RuntimeError(
                f"Drive image exceeds the bounded OCR pixel limit of {GOOGLE_DRIVE_OCR_MAX_PIXELS}."
            )
    content = _download_drive_media_bounded(
        drive_service,
        file_id,
        max_bytes=bounded_bytes,
    )
    content_sha256 = hashlib.sha256(content).hexdigest()
    if expected_content_sha256 and expected_content_sha256 != content_sha256:
        raise ValueError("Drive media changed since the preceding read; restart from page 1.")
    coverage: dict[str, Any] = {}
    text, extraction_method, blocker, pages_processed = _extract_drive_media_text(
        content,
        mime_type=mime_type,
        filename=str(metadata.get("name") or ""),
        max_pages=bounded_pages,
        start_page=start_page,
        coverage=coverage,
        max_raster_bytes=min(
            GOOGLE_DRIVE_OCR_MAX_RASTER_BYTES,
            max(25_000_000, bounded_bytes * 4),
        ),
    )
    normalized_text = "\n".join(line.rstrip() for line in str(text or "").splitlines()).strip()
    text_sha256 = hashlib.sha256(normalized_text.encode()).hexdigest()
    if expected_text_sha256 and expected_text_sha256 != text_sha256:
        raise ValueError("Media extraction changed since the preceding text window; restart it.")
    if start_char > len(normalized_text):
        raise ValueError("start_char exceeds the extracted page window.")
    returned_text = normalized_text[start_char : start_char + bounded_chars]
    end_char = start_char + len(returned_text)
    remaining_text = end_char < len(normalized_text)
    next_page = coverage.get("next_page")
    truncated = remaining_text or next_page is not None
    next_request = None
    if truncated:
        next_request = {
            "file_id_or_url": file_id,
            "folder_path": target_folder_path,
            "max_bytes": bounded_bytes,
            "max_pages": bounded_pages,
            "max_chars": bounded_chars,
            "start_page": start_page if remaining_text else next_page,
            "start_char": end_char if remaining_text else 0,
            "expected_content_sha256": content_sha256,
            "expected_text_sha256": text_sha256 if remaining_text else "",
        }
    return {
        "status": "success" if returned_text and not (blocker or truncated) else "partial",
        "operation": "read_drive_media_text",
        "file_id": file_id,
        "name": str(metadata.get("name") or ""),
        "mime_type": mime_type,
        "url": str(metadata.get("webViewLink") or ""),
        "modified_time": str(metadata.get("modifiedTime") or ""),
        "folder_path": target_folder_path,
        "text": returned_text,
        "char_count": len(returned_text),
        "truncated": truncated,
        "text_window": {"start_char": start_char, "end_char": end_char, "total_chars": len(normalized_text)},
        "text_sha256": text_sha256,
        "coverage": coverage,
        "content_complete": start_page == 1 and start_char == 0 and bool(returned_text) and not (blocker or truncated),
        "window_complete": bool(returned_text) and not (blocker or remaining_text),
        "next_request": next_request,
        "extraction_method": extraction_method,
        "pages_processed": pages_processed,
        "blocker": blocker,
        "content_sha256": content_sha256,
        "provider_bytes_downloaded": True,
        "source_identity_verified": True,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_drive_media_ocr_read(
    file_id_or_url: Annotated[str, Field(min_length=1, max_length=2_000)],
    folder_path: Annotated[str, Field(max_length=500)] = "",
    max_bytes: Annotated[int, Field(ge=1_000_000, le=25_000_000)] = 10_000_000,
    max_pages: Annotated[int, Field(ge=1, le=20)] = 8,
    max_chars: Annotated[int, Field(ge=500, le=30_000)] = 12_000,
    start_page: Annotated[int, Field(ge=1)] = 1,
    start_char: Annotated[int, Field(ge=0)] = 0,
    expected_content_sha256: Annotated[str, Field(max_length=64)] = "",
    expected_text_sha256: Annotated[str, Field(max_length=64)] = "",
    live: bool = False,
) -> str:
    """Read bounded text/OCR from one verified Drive PDF or image without mutation."""

    result = _google_workspace_read_call(
        "google_drive_media_ocr_read",
        "read_drive_media_text",
        lambda: google_drive_media_ocr_read_impl(
            file_id_or_url,
            folder_path=folder_path,
            max_bytes=max_bytes,
            max_pages=max_pages,
            max_chars=max_chars,
            start_page=start_page,
            start_char=start_char,
            expected_content_sha256=expected_content_sha256,
            expected_text_sha256=expected_text_sha256,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(result, ensure_ascii=True, sort_keys=True, default=str)


def _extract_drive_media_text(
    content: bytes,
    *,
    mime_type: str,
    filename: str,
    max_pages: int,
    max_raster_bytes: int = GOOGLE_DRIVE_OCR_MAX_RASTER_BYTES,
    start_page: int = 1,
    coverage: dict[str, Any] | None = None,
) -> tuple[str, str, str, int]:
    """Extract a bounded page window, retaining embedded and image text separately."""

    details = coverage if coverage is not None else {}
    details.update({
        "start_page": start_page, "page_count": None, "next_page": None,
        "pages": [], "scope": "text extraction; visual layout and figure meaning are not verified",
    })
    if mime_type == "application/pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            page_count = len(reader.pages)
            details["page_count"] = page_count
        except Exception as exc:
            reader = None
            details["parser_failure"] = type(exc).__name__

        pdftoppm = shutil.which(os.getenv("KEYSTONE_PDFTOPPM_COMMAND", "pdftoppm"))
        tesseract = shutil.which(os.getenv("KEYSTONE_TESSERACT_COMMAND", "tesseract"))
        chunks: list[str] = []
        failures: list[str] = []
        used_ocr = False
        raster_bytes = 0
        with tempfile.TemporaryDirectory(prefix="kba-drive-ocr-") as temporary_dir:
            root = Path(temporary_dir)
            source = root / "source.pdf"
            source.write_bytes(content)
            if reader is None:
                # Poppler can recover PDFs that pypdf cannot parse. Establish
                # page bounds independently before rasterizing a capped window.
                pdfinfo = shutil.which(os.getenv("KEYSTONE_PDFINFO_COMMAND", "pdfinfo"))
                if not pdfinfo or not pdftoppm or not tesseract:
                    return "", "pypdf", "PDF parsing failed; bounded OCR fallback requires pdfinfo, pdftoppm and tesseract.", 0
                try:
                    info = subprocess.run(
                        [pdfinfo, str(source)], text=True, capture_output=True,
                        timeout=15, check=False, env={**os.environ, "LC_ALL": "C"},
                    )
                    match = re.search(r"^Pages:\s+(\d+)\s*$", info.stdout, re.MULTILINE)
                    if info.returncode or not match or int(match[1]) < 1:
                        raise ValueError("PDF page count unavailable")
                    page_count = int(match[1])
                    details["page_count"] = page_count
                    details["page_count_source"] = "pdfinfo"
                except (OSError, subprocess.SubprocessError, ValueError) as exc:
                    return "", "pdfinfo", f"PDF fallback page bounds unavailable ({type(exc).__name__}).", 0
            if start_page > page_count:
                return "", "pypdf" if reader is not None else "pdfinfo", "Requested page is outside the PDF.", 0
            end_page = min(page_count, start_page + max_pages - 1)
            details["next_page"] = end_page + 1 if end_page < page_count else None
            for page_number in range(start_page, end_page + 1):
                page = reader.pages[page_number - 1] if reader is not None else None
                page_detail: dict[str, Any] = {"page": page_number, "embedded_text": False, "ocr_attempted": False}
                details["pages"].append(page_detail)
                try:
                    embedded = (page.extract_text() or "") if page is not None else ""
                except Exception:
                    embedded = ""
                try:
                    has_images = bool(page.images.keys()) if page is not None else True
                except Exception:
                    has_images = True
                    page_detail["image_inspection_failed"] = True
                    failures.append(f"Page {page_number}: image metadata unavailable; visual coverage remains uncertain.")
                page_detail["embedded_text"] = bool(embedded.strip())
                page_detail["has_images"] = has_images
                if embedded.strip():
                    chunks.append(f"[Page {page_number} — embedded text]\n{embedded.strip()}")
                if embedded.strip() and not has_images:
                    page_detail["status"] = "text_extracted"
                    continue
                if not pdftoppm or not tesseract:
                    page_detail["status"] = "ocr_unavailable"
                    failures.append(f"Page {page_number}: local OCR requires pdftoppm and tesseract.")
                    continue
                if raster_bytes >= max_raster_bytes:
                    page_detail["status"] = "raster_limit"
                    failures.append(f"Page {page_number}: local raster byte budget is exhausted.")
                    continue
                # Bound raster expansion before invoking the external renderer.
                pixel_limit = False
                if page is not None:
                    try:
                        width = float(page.mediabox.width) * 150 / 72
                        height = float(page.mediabox.height) * 150 / 72
                        pixel_limit = width <= 0 or height <= 0 or width * height > GOOGLE_DRIVE_OCR_MAX_PIXELS
                    except Exception:
                        pixel_limit = True
                if pixel_limit:
                    page_detail["status"] = "raster_limit"
                    failures.append(f"Page {page_number}: raster exceeds the OCR pixel limit.")
                    continue
                page_detail["ocr_attempted"] = True
                used_ocr = True
                output_prefix = root / f"page-{page_number}"
                image_path = output_prefix.with_suffix(".png")
                try:
                    raster_scale = (
                        ["-scale-to", str(int(GOOGLE_DRIVE_OCR_MAX_PIXELS ** 0.5))]
                        if page is None else ["-r", "150"]
                    )
                    _run_bounded_command(
                        [pdftoppm, "-f", str(page_number), "-l", str(page_number),
                         "-singlefile", *raster_scale, "-png", str(source), str(output_prefix)],
                        timeout=60,
                    )
                    raster_bytes += image_path.stat().st_size
                    if raster_bytes > max_raster_bytes:
                        raise RuntimeError("bounded raster byte limit")
                    result = subprocess.run(
                        [tesseract, str(image_path), "stdout"], text=True,
                        capture_output=True, timeout=30, check=False,
                    )
                    if result.returncode:
                        raise RuntimeError("nonzero OCR exit")
                    ocr_text = result.stdout.strip()
                    if not ocr_text:
                        raise RuntimeError("OCR returned no text; visual content remains unverified")
                    # Only exact normalized lines are redundant. Never infer that
                    # embedded text covers an image or that an OCR variant is equivalent.
                    seen = {" ".join(line.split()) for line in embedded.splitlines() if line.strip()}
                    supplemental = [line for line in ocr_text.splitlines() if " ".join(line.split()) not in seen]
                    if any(line.strip() for line in supplemental):
                        chunks.append(f"[Page {page_number} — OCR text]\n" + "\n".join(supplemental))
                    page_detail["status"] = "ocr_extracted"
                    page_detail["ocr_accuracy_verified"] = False
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    page_detail["status"] = "ocr_failed"
                    failures.append(f"Page {page_number}: OCR failed ({type(exc).__name__}).")
        details["failed_pages"] = [p["page"] for p in details["pages"] if p["status"] not in {"text_extracted", "ocr_extracted"}]
        details["ocr_accuracy_verified"] = False if used_ocr else None
        return "\n\n".join(chunks), "pypdf+pdftoppm+tesseract" if used_ocr else "pypdf", " ".join(failures), len(details["pages"])

    if mime_type.startswith("image/"):
        details["page_count"] = 1
        if start_page != 1:
            return "", "", "Images support only page 1.", 0
        tesseract = shutil.which(os.getenv("KEYSTONE_TESSERACT_COMMAND", "tesseract"))
        if not tesseract:
            return "", "", "Image OCR requires tesseract.", 0
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff"}:
            suffix = ".png"
        with tempfile.TemporaryDirectory(prefix="kba-drive-ocr-") as temporary_dir:
            source = Path(temporary_dir) / f"source{suffix}"
            source.write_bytes(content)
            try:
                result = subprocess.run(
                    [tesseract, str(source), "stdout"], text=True,
                    capture_output=True, timeout=30, check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return "", "tesseract", f"Image OCR failed: {type(exc).__name__}", 0
            if result.returncode != 0 or not result.stdout.strip():
                return "", "tesseract", "Image OCR failed or returned no readable text.", 0
            details["pages"] = [{"page": 1, "status": "ocr_extracted", "has_images": True, "ocr_accuracy_verified": False}]
            return result.stdout, "tesseract", "", 1
    return "", "", "Unsupported Drive media type.", 0


def _media_io_base_download(buffer: io.BytesIO, request: Any, *, chunksize: int) -> Any:
    """Build the Google API chunked downloader behind a patchable local seam."""

    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise RuntimeError(
            "Google Drive media reads require the google-api-python-client download helper."
        ) from exc
    return MediaIoBaseDownload(buffer, request, chunksize=chunksize)


def _download_drive_media_bounded(
    drive_service: Any,
    file_id: str,
    *,
    max_bytes: int,
) -> bytes:
    """Stream one Drive object and abort before buffering materially over the cap."""

    request = drive_service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = _media_io_base_download(
        buffer,
        request,
        chunksize=min(GOOGLE_DRIVE_MEDIA_DOWNLOAD_CHUNK_BYTES, max_bytes),
    )
    complete = False
    while not complete:
        _status, complete = downloader.next_chunk(num_retries=0)
        if buffer.tell() > max_bytes:
            raise RuntimeError(f"Drive media exceeds the bounded {max_bytes}-byte download limit.")
    return buffer.getvalue()


def _google_drive_folder_readback(
    drive_service: Any,
    folder_id: str,
    *,
    expected_name: str = "",
    expected_trashed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read back one folder identity after a mutation without hiding uncertainty."""

    try:
        payload = (
            drive_service.files()
            .get(
                fileId=folder_id,
                fields="id,name,mimeType,trashed,parents,webViewLink",
            )
            .execute()
        )
    except Exception as exc:
        return {}, {
            "status": "pending_readback",
            "passed": False,
            "folder_id_match": False,
            "name_match": False if expected_name else None,
            "mime_type_match": False,
            "trashed_match": False,
            "blocker": (
                "Google Drive mutation completed, but provider read-back failed: "
                f"{type(exc).__name__}: {exc}"
            )[:700],
        }
    metadata = dict(payload) if isinstance(payload, Mapping) else {}
    folder_id_match = str(metadata.get("id") or "") == folder_id
    name_match = str(metadata.get("name") or "") == expected_name if expected_name else True
    mime_type_match = str(metadata.get("mimeType") or "") == GOOGLE_FOLDER_MIME_TYPE
    trashed_match = bool(metadata.get("trashed")) is expected_trashed
    passed = folder_id_match and name_match and mime_type_match and trashed_match
    return metadata, {
        "status": "verified" if passed else "verification_failed",
        "passed": passed,
        "folder_id_match": folder_id_match,
        "name_match": name_match,
        "mime_type_match": mime_type_match,
        "trashed_match": trashed_match,
        "blocker": (
            ""
            if passed
            else "Google Drive read-back did not verify the exact folder identity, "
            "name, type, and lifecycle state."
        ),
    }


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
            "operation": "create_folder",
            "folder_path": target_folder_path,
            "approval_reference": approval_reference.strip(),
            "provider_write": False,
            "provider_mutated": False,
            "send_enabled": False,
        }
    _require_google_workspace_write_approval(approval_reference)
    services = _google_workspace_services()
    drive_service = services["drive"]
    _assert_configured_google_account(drive_service)
    existing_folder_id = _find_drive_folder_path(drive_service, target_folder_path)
    folder_id = _ensure_drive_folder_path(drive_service, target_folder_path)
    expected_name = target_folder_path.rsplit("/", 1)[-1].strip()
    metadata, verification = _google_drive_folder_readback(
        drive_service,
        folder_id,
        expected_name=expected_name,
        expected_trashed=False,
    )
    provider_write = not bool(existing_folder_id)
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "create_folder",
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "name": str(metadata.get("name") or expected_name),
        "provider_link": str(metadata.get("webViewLink") or "")
        or f"https://drive.google.com/drive/folders/{folder_id}",
        "approval_reference": approval_reference.strip(),
        "provider_write": provider_write,
        "provider_mutated": provider_write,
        "verification": verification,
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
            "operation": "rename_folder",
            "folder_path_or_id": scoped_target,
            "new_name": cleaned_name,
            "approval_reference": approval_reference.strip(),
            "provider_write": False,
            "provider_mutated": False,
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
    readback, verification = _google_drive_folder_readback(
        drive_service,
        folder_id,
        expected_name=cleaned_name,
        expected_trashed=False,
    )
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "rename_folder",
        "folder_id": folder_id,
        "name": str(readback.get("name") or cleaned_name),
        "provider_link": str(
            readback.get("webViewLink")
            or (metadata.get("webViewLink") if isinstance(metadata, dict) else "")
            or ""
        ),
        "approval_reference": approval_reference.strip(),
        "provider_write": True,
        "provider_mutated": True,
        "verification": verification,
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
            "operation": "remove_folder",
            "folder_path_or_id": scoped_target,
            "approval_reference": approval_reference.strip(),
            "provider_write": False,
            "provider_mutated": False,
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
    readback, verification = _google_drive_folder_readback(
        drive_service,
        folder_id,
        expected_name=(str(metadata.get("name") or "") if isinstance(metadata, dict) else ""),
        expected_trashed=True,
    )
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": "remove_folder",
        "folder_id": folder_id,
        "name": str(
            readback.get("name")
            or (metadata.get("name") if isinstance(metadata, dict) else "")
            or ""
        ),
        "trashed": bool(readback.get("trashed")),
        "provider_link": str(
            readback.get("webViewLink")
            or (metadata.get("webViewLink") if isinstance(metadata, dict) else "")
            or ""
        ),
        "approval_reference": approval_reference.strip(),
        "provider_write": True,
        "provider_mutated": True,
        "verification": verification,
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

    result = _google_workspace_read_call(
        "google_sheet_list",
        "list_sheets",
        lambda: google_sheet_list_impl(
            folder_path,
            max_items=max_items,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@durable_provider_tool("google_sheet_create")
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
    record_provider_observation(
        {
            "status": "observed",
            "provider": "google_workspace",
            "operation": "create_sheet",
            "spreadsheet_id": spreadsheet_id,
            "provider_write": True,
            "verification": {"passed": False, "status": "pending_readback"},
        }
    )
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


def _google_sheet_column_index(column: str) -> int:
    value = str(column or "").replace("$", "").upper()
    if not value or not value.isalpha():
        raise ValueError(f"Invalid Google Sheets column reference: {column!r}")
    index = 0
    for char in value:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _google_sheet_column_name(index: int) -> str:
    if index < 0:
        raise ValueError("Google Sheets column index must be non-negative.")
    chars: list[str] = []
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))


def _split_google_sheet_a1_range(range_a1: str) -> tuple[str, str]:
    value = str(range_a1 or "")
    if not value:
        return "", ""
    if value.startswith("'"):
        index = 1
        title: list[str] = []
        while index < len(value):
            char = value[index]
            if char == "'" and index + 1 < len(value) and value[index + 1] == "'":
                title.append("'")
                index += 2
                continue
            if char == "'" and index + 1 < len(value) and value[index + 1] == "!":
                return "".join(title), value[index + 2 :]
            title.append(char)
            index += 1
        raise ValueError("Quoted Google Sheets A1 range has no closing sheet qualifier.")
    if "!" not in value:
        return "", value
    sheet_title, cells = value.rsplit("!", 1)
    return sheet_title, cells


def _google_sheet_a1_bounds(
    cells_a1: str,
    *,
    grid_row_count: int,
    grid_column_count: int,
) -> dict[str, int | bool]:
    cleaned = str(cells_a1 or "").replace("$", "")
    match = re.fullmatch(
        r"(?P<start_col>[A-Za-z]*)(?P<start_row>\d*)"
        r"(?::(?P<end_col>[A-Za-z]*)(?P<end_row>\d*))?",
        cleaned,
    )
    if not match or not any(match.groupdict().values()):
        raise ValueError(f"Unsupported or empty Google Sheets A1 range: {cells_a1!r}")
    start_col_text = match.group("start_col")
    start_row_text = match.group("start_row")
    end_col_text = match.group("end_col")
    end_row_text = match.group("end_row")
    has_separator = ":" in cleaned
    start_column = _google_sheet_column_index(start_col_text) if start_col_text else 0
    start_row = int(start_row_text) - 1 if start_row_text else 0
    if start_row < 0:
        raise ValueError("Google Sheets A1 rows are one-based.")
    if has_separator:
        end_column = (
            _google_sheet_column_index(end_col_text) + 1
            if end_col_text
            else max(grid_column_count, start_column + 1)
        )
        end_row = int(end_row_text) if end_row_text else max(grid_row_count, start_row + 1)
    else:
        end_column = start_column + 1 if start_col_text else max(grid_column_count, 1)
        end_row = start_row + 1 if start_row_text else max(grid_row_count, 1)
    if end_column <= start_column or end_row <= start_row:
        raise ValueError("Google Sheets A1 range resolves to an empty or reversed rectangle.")
    return {
        "start_row": start_row,
        "end_row": end_row,
        "start_column": start_column,
        "end_column": end_column,
        "row_end_bounded_by_source": bool(end_row_text or not has_separator),
        "column_end_bounded_by_source": bool(end_col_text or not has_separator),
    }


def _google_sheet_grid_range_a1(
    *,
    sheet_name: str,
    start_row: int,
    end_row: int,
    start_column: int,
    end_column: int,
) -> str:
    return (
        f"{_sheet_name_a1(sheet_name)}!"
        f"{_google_sheet_column_name(start_column)}{start_row + 1}:"
        f"{_google_sheet_column_name(end_column - 1)}{end_row}"
    )


def _google_sheet_source_identity(
    metadata: Mapping[str, Any],
    *,
    spreadsheet_id: str,
) -> dict[str, Any]:
    properties = metadata.get("properties")
    workbook_properties = properties if isinstance(properties, Mapping) else {}
    sheets: list[dict[str, Any]] = []
    for item in metadata.get("sheets", []):
        if not isinstance(item, Mapping):
            continue
        raw_properties = item.get("properties")
        if not isinstance(raw_properties, Mapping):
            continue
        grid = raw_properties.get("gridProperties")
        grid_properties = grid if isinstance(grid, Mapping) else {}
        sheets.append(
            {
                "sheet_id": raw_properties.get("sheetId"),
                "title": str(raw_properties.get("title") or ""),
                "row_count": int(grid_properties.get("rowCount") or 0),
                "column_count": int(grid_properties.get("columnCount") or 0),
            }
        )
    named_ranges: list[dict[str, Any]] = []
    for item in metadata.get("namedRanges", []):
        if not isinstance(item, Mapping):
            continue
        raw_range = item.get("range")
        grid_range = dict(raw_range) if isinstance(raw_range, Mapping) else {}
        named_ranges.append(
            {
                "named_range_id": str(item.get("namedRangeId") or ""),
                "name": str(item.get("name") or ""),
                "range": grid_range,
            }
        )
    identity_basis = {
        "spreadsheet_id": str(metadata.get("spreadsheetId") or spreadsheet_id),
        "title": str(workbook_properties.get("title") or ""),
        "locale": str(workbook_properties.get("locale") or ""),
        "time_zone": str(workbook_properties.get("timeZone") or ""),
        "sheets": sheets,
        "named_ranges": named_ranges,
    }
    serialized = json.dumps(
        identity_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        **identity_basis,
        "identity_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "stability": "metadata_identity_only_not_cell_revision",
    }


def _google_sheet_resolve_range(
    *,
    requested_range: str,
    requested_sheet_name: str,
    source_identity: Mapping[str, Any],
    metadata_payload: Mapping[str, Any],
    fallback_resolved_range: str = "",
) -> dict[str, Any]:
    sheets = [item for item in source_identity.get("sheets", []) if isinstance(item, Mapping)]
    by_id = {int(item.get("sheet_id")): item for item in sheets if item.get("sheet_id") is not None}
    range_sheet_name, cells_a1 = _split_google_sheet_a1_range(requested_range)
    named_range_name = ""
    named_range_id = ""
    selected_sheet: Mapping[str, Any] | None = None
    bounds: dict[str, int | bool]

    if requested_range and not range_sheet_name:
        matches = [
            item
            for item in source_identity.get("named_ranges", [])
            if isinstance(item, Mapping) and item.get("name") == requested_range
        ]
        if len(matches) == 1:
            named = matches[0]
            named_range_name = requested_range
            named_range_id = str(named.get("named_range_id") or "")
            raw_grid = named.get("range")
            grid = raw_grid if isinstance(raw_grid, Mapping) else {}
            selected_sheet = (
                by_id.get(int(grid.get("sheetId"))) if grid.get("sheetId") is not None else None
            )
            if selected_sheet is None:
                return {"status": "named_range_sheet_not_found"}
            if requested_sheet_name and selected_sheet.get("title") != requested_sheet_name:
                return {"status": "named_range_sheet_conflict"}
            start_row = int(grid.get("startRowIndex") or 0)
            start_column = int(grid.get("startColumnIndex") or 0)
            bounds = {
                "start_row": start_row,
                "end_row": int(
                    grid.get("endRowIndex") or selected_sheet.get("row_count") or start_row + 1
                ),
                "start_column": start_column,
                "end_column": int(
                    grid.get("endColumnIndex")
                    or selected_sheet.get("column_count")
                    or start_column + 1
                ),
                "row_end_bounded_by_source": grid.get("endRowIndex") is not None,
                "column_end_bounded_by_source": grid.get("endColumnIndex") is not None,
            }
        elif len(matches) > 1:
            return {"status": "named_range_ambiguous"}
        elif requested_sheet_name:
            range_sheet_name = requested_sheet_name
        elif fallback_resolved_range:
            fallback_sheet, fallback_cells = _split_google_sheet_a1_range(fallback_resolved_range)
            if not fallback_sheet:
                return {"status": "named_range_resolution_unavailable"}
            named_range_name = requested_range
            range_sheet_name = fallback_sheet
            cells_a1 = fallback_cells
        else:
            return {"status": "named_range_not_found"}
    elif not requested_range:
        range_sheet_name = requested_sheet_name
        cells_a1 = ""

    if selected_sheet is None:
        matches = [item for item in sheets if item.get("title") == range_sheet_name]
        if len(matches) > 1:
            return {"status": "sheet_ambiguous"}
        if matches:
            selected_sheet = matches[0]
        elif range_sheet_name:
            selected_sheet = {
                "sheet_id": None,
                "title": range_sheet_name,
                "row_count": 0,
                "column_count": 0,
            }
        else:
            return {"status": "sheet_identity_required"}
        if not cells_a1:
            bounds = {
                "start_row": 0,
                "end_row": max(int(selected_sheet.get("row_count") or 0), 1),
                "start_column": 0,
                "end_column": max(int(selected_sheet.get("column_count") or 0), 1),
                "row_end_bounded_by_source": False,
                "column_end_bounded_by_source": False,
            }
        else:
            bounds = _google_sheet_a1_bounds(
                cells_a1,
                grid_row_count=int(selected_sheet.get("row_count") or 0),
                grid_column_count=int(selected_sheet.get("column_count") or 0),
            )

    sheet_title = str(selected_sheet.get("title") or range_sheet_name)
    full_range = _google_sheet_grid_range_a1(
        sheet_name=sheet_title,
        start_row=int(bounds["start_row"]),
        end_row=int(bounds["end_row"]),
        start_column=int(bounds["start_column"]),
        end_column=int(bounds["end_column"]),
    )
    return {
        "status": "success",
        "sheet_id": selected_sheet.get("sheet_id"),
        "sheet_name": sheet_title,
        "named_range_name": named_range_name,
        "named_range_id": named_range_id,
        "requested_range": requested_range,
        "resolved_range": full_range,
        "bounds": bounds,
        "metadata_basis": (
            "spreadsheets.get"
            if source_identity.get("sheets")
            else "provider_resolved_value_range_fallback"
        ),
    }


def _google_sheet_public_source_identity(
    source_identity: Mapping[str, Any],
    *,
    resolution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_sheet_id = resolution.get("sheet_id") if resolution else None
    selected_sheet_name = str(resolution.get("sheet_name") or "") if resolution else ""
    selected_sheet = next(
        (
            dict(item)
            for item in source_identity.get("sheets", [])
            if isinstance(item, Mapping)
            and (
                item.get("sheet_id") == selected_sheet_id
                if selected_sheet_id is not None
                else item.get("title") == selected_sheet_name
            )
        ),
        {},
    )
    return {
        "spreadsheet_id": str(source_identity.get("spreadsheet_id") or ""),
        "title": str(source_identity.get("title") or ""),
        "locale": str(source_identity.get("locale") or ""),
        "time_zone": str(source_identity.get("time_zone") or ""),
        "selected_sheet": selected_sheet,
        "selected_named_range": (
            {
                "name": str(resolution.get("named_range_name") or ""),
                "named_range_id": str(resolution.get("named_range_id") or ""),
            }
            if resolution and resolution.get("named_range_name")
            else {}
        ),
        "sheet_count": len(source_identity.get("sheets", [])),
        "named_range_count": len(source_identity.get("named_ranges", [])),
        "identity_sha256": str(source_identity.get("identity_sha256") or ""),
        "stability": str(source_identity.get("stability") or ""),
    }


def _google_sheet_value_at(rows: object, row_index: int, column_index: int) -> tuple[bool, Any]:
    if not isinstance(rows, list) or row_index >= len(rows):
        return False, None
    row = rows[row_index]
    if not isinstance(row, list) or column_index >= len(row):
        return False, None
    return True, row[column_index]


def _google_sheet_bounded_scalar(
    value: Any,
    *,
    start_char: int,
    max_chars: int,
) -> tuple[Any, dict[str, Any] | None]:
    if not isinstance(value, str):
        return value, None
    effective_start = min(start_char, len(value))
    end = min(len(value), effective_start + max_chars)
    preview = value[effective_start:end]
    coverage = {
        "start": effective_start,
        "end": end,
        "full_count": len(value),
        "complete": end >= len(value),
        "has_more": end < len(value),
    }
    return preview, coverage if start_char or end < len(value) else None


def _google_sheet_typed_value(
    value: object,
    *,
    start_char: int,
    max_chars: int,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not value:
        return {"state": "missing", "type": "missing", "value": None}
    for key, value_type in (
        ("formulaValue", "formula"),
        ("numberValue", "number"),
        ("stringValue", "string"),
        ("boolValue", "boolean"),
        ("errorValue", "error"),
    ):
        if key in value:
            raw_value = value.get(key)
            if value_type == "error" and isinstance(raw_value, Mapping):
                raw_message = str(raw_value.get("message") or "")
                message, coverage = _google_sheet_bounded_scalar(
                    raw_message,
                    start_char=start_char,
                    max_chars=max_chars,
                )
                error_value = {
                    "type": str(raw_value.get("type") or ""),
                    "message": message,
                }
                result = {"type": value_type, "value": error_value}
                if coverage:
                    result["coverage"] = coverage
                return result
            bounded_value, coverage = _google_sheet_bounded_scalar(
                raw_value,
                start_char=start_char,
                max_chars=max_chars,
            )
            result = {"type": value_type, "value": bounded_value}
            if coverage:
                result["coverage"] = coverage
            return result
    return {"type": "unknown", "value": dict(value)}


def _google_sheet_cell_links(cell: Mapping[str, Any]) -> list[str]:
    links: list[str] = []
    direct = str(cell.get("hyperlink") or "")
    if direct:
        links.append(direct)
    raw_format = cell.get("userEnteredFormat")
    if isinstance(raw_format, Mapping):
        text_format = raw_format.get("textFormat")
        if isinstance(text_format, Mapping) and isinstance(text_format.get("link"), Mapping):
            uri = str(text_format["link"].get("uri") or "")
            if uri and uri not in links:
                links.append(uri)
    for run in cell.get("textFormatRuns", []):
        if not isinstance(run, Mapping):
            continue
        run_format = run.get("format")
        if not isinstance(run_format, Mapping) or not isinstance(run_format.get("link"), Mapping):
            continue
        uri = str(run_format["link"].get("uri") or "")
        if uri and uri not in links:
            links.append(uri)
    return links


def _google_sheet_grid_projection(
    payload: Mapping[str, Any],
    *,
    sheet_id: object,
    page_bounds: Mapping[str, int],
    note_start_char: int,
    max_note_chars: int,
    link_start: int,
    max_links: int,
    value_start_char: int,
    max_value_chars: int,
) -> dict[str, Any] | None:
    sheet_payload: Mapping[str, Any] | None = None
    for item in payload.get("sheets", []):
        if not isinstance(item, Mapping):
            continue
        properties = item.get("properties")
        if not isinstance(properties, Mapping):
            continue
        if sheet_id is None or properties.get("sheetId") == sheet_id:
            sheet_payload = item
            break
    if sheet_payload is None:
        return None
    data_blocks = [item for item in sheet_payload.get("data", []) if isinstance(item, Mapping)]
    cells: list[dict[str, Any]] = []
    rows_by_index: dict[int, dict[int, Any]] = {}
    for block in data_blocks:
        block_start_row = (
            int(block["startRow"])
            if isinstance(block.get("startRow"), int)
            else page_bounds["start_row"]
        )
        block_start_column = (
            int(block["startColumn"])
            if isinstance(block.get("startColumn"), int)
            else page_bounds["start_column"]
        )
        for row_offset, row_data in enumerate(block.get("rowData", [])):
            if not isinstance(row_data, Mapping):
                continue
            absolute_row = block_start_row + row_offset
            if not page_bounds["start_row"] <= absolute_row < page_bounds["end_row"]:
                continue
            for column_offset, raw_cell in enumerate(row_data.get("values", [])):
                if not isinstance(raw_cell, Mapping) or not raw_cell:
                    continue
                absolute_column = block_start_column + column_offset
                if not (
                    page_bounds["start_column"]
                    <= absolute_column
                    < page_bounds["end_column"]
                ):
                    continue
                coordinate = f"{_google_sheet_column_name(absolute_column)}{absolute_row + 1}"
                entered = _google_sheet_typed_value(
                    raw_cell.get("userEnteredValue"),
                    start_char=value_start_char,
                    max_chars=max_value_chars,
                )
                effective = _google_sheet_typed_value(
                    raw_cell.get("effectiveValue"),
                    start_char=value_start_char,
                    max_chars=max_value_chars,
                )
                raw_note = str(raw_cell.get("note") or "")
                note_end = min(len(raw_note), note_start_char + max_note_chars)
                note_preview = raw_note[note_start_char:note_end]
                formatted_present = "formattedValue" in raw_cell
                raw_formatted_value = (
                    raw_cell.get("formattedValue") if formatted_present else None
                )
                formatted_value, formatted_coverage = _google_sheet_bounded_scalar(
                    raw_formatted_value,
                    start_char=value_start_char,
                    max_chars=max_value_chars,
                )
                all_links = _google_sheet_cell_links(raw_cell)
                visible_links = all_links[link_start : link_start + max_links]
                rows_by_index.setdefault(absolute_row, {})[absolute_column] = (
                    formatted_value if formatted_present else ""
                )
                cell_projection: dict[str, Any] = {
                    "coordinate": coordinate,
                    "entered": entered,
                    "effective": effective,
                    "display": formatted_value if formatted_present else None,
                    "formula_provenance": (
                        "formula"
                        if entered.get("type") == "formula"
                        else "literal"
                        if entered.get("state") != "missing"
                        else "unset"
                    ),
                }
                value_fields = {
                    key: coverage
                    for key, coverage in (
                        ("entered", entered.get("coverage")),
                        ("effective", effective.get("coverage")),
                        ("display", formatted_coverage),
                    )
                    if isinstance(coverage, Mapping)
                }
                if value_fields:
                    cell_projection["value_coverage"] = {
                        "requested_start": value_start_char,
                        "fields": value_fields,
                        "complete": not any(
                            coverage.get("has_more") is True
                            for coverage in value_fields.values()
                        ),
                        "has_more": any(
                            coverage.get("has_more") is True
                            for coverage in value_fields.values()
                        ),
                    }
                raw_effective_format = raw_cell.get("effectiveFormat")
                effective_format = (
                    raw_effective_format if isinstance(raw_effective_format, Mapping) else {}
                )
                raw_number_format = effective_format.get("numberFormat")
                if isinstance(raw_number_format, Mapping):
                    cell_projection["number_format"] = {
                        "type": str(raw_number_format.get("type") or ""),
                        "pattern": str(raw_number_format.get("pattern") or ""),
                    }
                if raw_note or note_start_char:
                    cell_projection["note"] = note_preview
                    cell_projection["note_coverage"] = {
                        "start": note_start_char,
                        "end": note_end,
                        "full_count": len(raw_note),
                        "complete": note_end >= len(raw_note),
                        "has_more": note_end < len(raw_note),
                    }
                if all_links or link_start:
                    cell_projection["links"] = visible_links
                    cell_projection["link_coverage"] = {
                        "start": link_start,
                        "end": link_start + len(visible_links),
                        "full_count": len(all_links),
                        "complete": link_start + len(visible_links) >= len(all_links),
                        "has_more": link_start + len(visible_links) < len(all_links),
                    }
                cells.append(cell_projection)
    formatted_rows: list[list[Any]] = []
    if rows_by_index:
        last_row = max(rows_by_index)
        for absolute_row in range(page_bounds["start_row"], last_row + 1):
            columns = rows_by_index.get(absolute_row, {})
            if not columns:
                formatted_rows.append([])
                continue
            last_column = max(columns)
            formatted_rows.append(
                [
                    columns.get(absolute_column, "")
                    for absolute_column in range(page_bounds["start_column"], last_column + 1)
                ]
            )
    present_coordinates = {cell["coordinate"] for cell in cells}
    missing_cells = [
        f"{_google_sheet_column_name(column)}{row + 1}"
        for row in range(page_bounds["start_row"], page_bounds["end_row"])
        for column in range(page_bounds["start_column"], page_bounds["end_column"])
        if f"{_google_sheet_column_name(column)}{row + 1}" not in present_coordinates
    ]
    return {
        "rows": formatted_rows,
        "cells": cells,
        "missing_cells": missing_cells,
        "representations": {
            "mode": "source",
            "canonical_source_cells": "cells",
            "formatted_rows": "rows",
        },
        "representation_coverage": {
            "entered_values": True,
            "effective_values": True,
            "formatted_values": True,
            "formula_provenance": True,
            "missing_vs_entered_blank": True,
            "notes": True,
            "links": True,
            "complete": not any(
                (
                    isinstance(cell.get("note_coverage"), Mapping)
                    and cell["note_coverage"].get("has_more") is True
                )
                or (
                    isinstance(cell.get("link_coverage"), Mapping)
                    and cell["link_coverage"].get("has_more") is True
                )
                or (
                    isinstance(cell.get("value_coverage"), Mapping)
                    and cell["value_coverage"].get("has_more") is True
                )
                for cell in cells
            ),
        },
        "limitations": (
            [
                "One or more cell notes or link lists exceed the bounded preview; use the cell continuation request to inspect later source detail."
            ]
            if any(
                (
                    isinstance(cell.get("note_coverage"), Mapping)
                    and cell["note_coverage"].get("has_more") is True
                )
                or (
                    isinstance(cell.get("link_coverage"), Mapping)
                    and cell["link_coverage"].get("has_more") is True
                )
                or (
                    isinstance(cell.get("value_coverage"), Mapping)
                    and cell["value_coverage"].get("has_more") is True
                )
                for cell in cells
            )
            else []
        ),
    }


def _google_sheet_values_projection(
    sheets_service: Any,
    *,
    spreadsheet_id: str,
    read_range: str,
    representation: str,
    page_bounds: Mapping[str, int],
    value_start_char: int,
    max_value_chars: int,
) -> dict[str, Any]:
    modes = (
        (
            ("FORMATTED_VALUE", "formatted_rows"),
            ("UNFORMATTED_VALUE", "effective_rows"),
            ("FORMULA", "formula_or_literal_rows"),
        )
        if representation == "source"
        else (
            (
                {
                    "formatted": "FORMATTED_VALUE",
                    "unformatted": "UNFORMATTED_VALUE",
                    "formula": "FORMULA",
                }[representation],
                f"{representation}_rows",
            ),
        )
    )
    payloads: dict[str, dict[str, Any]] = {}
    raw_representations: dict[str, Any] = {}
    for render_option, key in modes:
        kwargs: dict[str, Any] = {
            "spreadsheetId": spreadsheet_id,
            "range": read_range,
            "majorDimension": "ROWS",
            "valueRenderOption": render_option,
        }
        if render_option == "UNFORMATTED_VALUE":
            kwargs["dateTimeRenderOption"] = "SERIAL_NUMBER"
        payload = sheets_service.spreadsheets().values().get(**kwargs).execute()
        safe_payload = dict(payload) if isinstance(payload, Mapping) else {}
        payloads[key] = safe_payload
        raw_rows = safe_payload.get("values", [])
        row_limit = page_bounds["end_row"] - page_bounds["start_row"]
        column_limit = page_bounds["end_column"] - page_bounds["start_column"]
        raw_representations[key] = (
            [
                list(row[:column_limit]) if isinstance(row, list) else []
                for row in raw_rows[:row_limit]
            ]
            if isinstance(raw_rows, list)
            else []
        )
    primary_key = (
        "formatted_rows"
        if "formatted_rows" in raw_representations
        else next(key for key in raw_representations if key.endswith("_rows"))
    )
    raw_rows = raw_representations.get(primary_key, [])
    rows = (
        [
            [
                _google_sheet_bounded_scalar(
                    value,
                    start_char=value_start_char,
                    max_chars=max_value_chars,
                )[0]
                for value in row
            ]
            for row in raw_rows
            if isinstance(row, list)
        ]
        if isinstance(raw_rows, list)
        else []
    )
    max_row_count = max(
        (
            len(value)
            for key, value in raw_representations.items()
            if key.endswith("_rows") and isinstance(value, list)
        ),
        default=0,
    )
    cells: list[dict[str, Any]] = []
    for row_offset in range(max_row_count):
        max_column_count = max(
            (
                len(value[row_offset])
            for key, value in raw_representations.items()
                if key.endswith("_rows")
                and isinstance(value, list)
                and row_offset < len(value)
                and isinstance(value[row_offset], list)
            ),
            default=0,
        )
        for column_offset in range(max_column_count):
            values: dict[str, Any] = {}
            value_fields: dict[str, Any] = {}
            for key, matrix in raw_representations.items():
                if not key.endswith("_rows"):
                    continue
                present, value = _google_sheet_value_at(matrix, row_offset, column_offset)
                if present:
                    bounded_value, coverage = _google_sheet_bounded_scalar(
                        value,
                        start_char=value_start_char,
                        max_chars=max_value_chars,
                    )
                    values[key] = bounded_value
                    if coverage:
                        value_fields[key] = coverage
            absolute_row = page_bounds["start_row"] + row_offset
            absolute_column = page_bounds["start_column"] + column_offset
            cell_projection: dict[str, Any] = {
                "coordinate": f"{_google_sheet_column_name(absolute_column)}{absolute_row + 1}",
                "values": values,
                "formula_provenance": "unavailable_from_values_api",
            }
            if value_fields:
                cell_projection["value_coverage"] = {
                    "requested_start": value_start_char,
                    "fields": value_fields,
                    "complete": not any(
                        coverage.get("has_more") is True
                        for coverage in value_fields.values()
                    ),
                    "has_more": any(
                        coverage.get("has_more") is True
                        for coverage in value_fields.values()
                    ),
                }
            cells.append(cell_projection)
    source_mode = representation == "source"
    output_representations: dict[str, Any] = {"mode": representation}
    for key in raw_representations:
        output_representations[key] = (
            "rows" if key == primary_key else f"cells[].values.{key}"
        )
    values_complete = not any(
        isinstance(cell.get("value_coverage"), Mapping)
        and cell["value_coverage"].get("has_more") is True
        for cell in cells
    )
    return {
        "rows": rows,
        "cells": cells,
        "missing_cells": [],
        "representations": output_representations,
        "representation_coverage": {
            "entered_values": False,
            "effective_values": source_mode or representation == "unformatted",
            "formatted_values": source_mode or representation == "formatted",
            "formula_or_literal_view": source_mode or representation == "formula",
            "formula_provenance": False,
            "missing_vs_entered_blank": False,
            "notes": False,
            "links": False,
            "requested_representation_complete": values_complete,
            "complete": False,
        },
        "provider_resolved_ranges": {
            key: str(payload.get("range") or "") for key, payload in payloads.items()
        },
        "limitations": (
            [
                "Grid cell metadata was unavailable; values representations are present, but formula-versus-literal provenance, notes, and links remain uninspected."
            ]
            if source_mode
            else [
                "Only one requested values representation was inspected; other source representations, notes, and links remain uninspected."
            ]
        )
        + (
            [
                "One or more cell values exceed the bounded preview; use the exact cell-value continuation request to inspect later source detail."
            ]
            if not values_complete
            else []
        ),
    }


def google_sheet_read_table_impl(
    spreadsheet_id_or_url: str = "",
    *,
    title: str = "",
    folder_path: str = "",
    sheet_name: str = "",
    range_a1: str = "",
    max_rows: int = 100,
    max_columns: int = 20,
    max_cells: int = GOOGLE_SHEET_READ_MAX_CELLS,
    row_start: int = 0,
    column_start: int = 0,
    row_band_start: int = -1,
    row_band_rows: int = 0,
    column_band_start: int = -1,
    column_band_columns: int = 0,
    row_chunk_rows: int = 0,
    representation: Literal["source", "formatted", "unformatted", "formula"] = "source",
    note_start_char: int = 0,
    max_note_chars: int = GOOGLE_SHEET_CELL_NOTE_PREVIEW_CHARS,
    link_start: int = 0,
    max_links: int = 10,
    value_start_char: int = 0,
    max_value_chars: int = GOOGLE_SHEET_CELL_VALUE_PREVIEW_CHARS,
    expected_source_identity_sha256: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Read one bounded, coordinate-preserving Google Sheets range window."""

    spreadsheet_id = _google_sheet_id(spreadsheet_id_or_url)
    if not spreadsheet_id and not str(title or "").strip():
        raise ValueError(
            "Provide spreadsheet_id_or_url or an explicit spreadsheet title; "
            "read-only calls do not silently choose the configured default workbook."
        )
    if not str(range_a1 or "").strip() and not str(sheet_name or "").strip():
        raise ValueError(
            "Provide sheet_name or range_a1; read-only calls do not silently choose a tab."
        )
    cleaned_title = _clean_spreadsheet_title(title) if str(title or "").strip() else ""
    target_folder_path = _google_docs_folder_path(folder_path)
    requested_sheet_name = str(sheet_name or "")
    if requested_sheet_name and not requested_sheet_name.strip():
        requested_sheet_name = ""
    if requested_sheet_name:
        if len(requested_sheet_name) > 100:
            raise ValueError("sheet_name must be 100 characters or fewer.")
        if any(char in requested_sheet_name for char in ("[", "]", "*", "?", "/", "\\")):
            raise ValueError("sheet_name contains characters Google Sheets does not allow.")
    requested_range = str(range_a1 or "")
    bounded_rows = min(max(int(max_rows or 100), 1), GOOGLE_SHEET_READ_MAX_ROWS)
    bounded_columns = min(max(int(max_columns or 20), 1), GOOGLE_SHEET_READ_MAX_COLUMNS)
    bounded_cells = min(
        max(int(max_cells or GOOGLE_SHEET_READ_MAX_CELLS), 1),
        GOOGLE_SHEET_READ_MAX_CELLS,
    )
    bounded_row_start = max(int(row_start or 0), 0)
    bounded_column_start = max(int(column_start or 0), 0)
    bounded_row_band_start = int(row_band_start)
    bounded_row_band_rows = min(max(int(row_band_rows or 0), 0), bounded_rows)
    bounded_column_band_start = int(column_band_start)
    bounded_column_band_columns = min(
        max(int(column_band_columns or 0), 0), bounded_columns
    )
    bounded_row_chunk_rows = min(max(int(row_chunk_rows or 0), 0), bounded_rows)
    bounded_note_start = max(int(note_start_char or 0), 0)
    bounded_note_chars = min(max(int(max_note_chars or 1), 1), 2_000)
    bounded_link_start = max(int(link_start or 0), 0)
    bounded_links = min(max(int(max_links or 1), 1), 100)
    bounded_value_start = max(int(value_start_char or 0), 0)
    bounded_value_chars = min(max(int(max_value_chars or 1), 1), 2_000)
    if representation not in {"source", "formatted", "unformatted", "formula"}:
        raise ValueError("representation must be source, formatted, unformatted, or formula")
    preview_range = requested_range or (
        f"{_sheet_name_a1(requested_sheet_name)}!A1:"
        f"{_google_sheet_column_name(bounded_columns - 1)}{bounded_rows}"
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": "read_table",
            "provider_read": False,
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": requested_sheet_name,
            "range": preview_range,
            "requested_range": requested_range,
            "representation": representation,
            "max_cells": bounded_cells,
            "note_start_char": bounded_note_start,
            "max_note_chars": bounded_note_chars,
            "link_start": bounded_link_start,
            "max_links": bounded_links,
            "value_start_char": bounded_value_start,
            "max_value_chars": bounded_value_chars,
            "rows": [],
            "row_count": 0,
            "cells": [],
            "content_complete": False,
            "semantic_complete": False,
            "coverage": {
                "status": "dry_run",
                "row_start": bounded_row_start,
                "column_start": bounded_column_start,
            },
            "continuation": {"available": False, "next_request": None},
            "limitations": ["No Google Sheets provider read was made."],
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
            "provider_read": False,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "rows": [],
            "row_count": 0,
            "content_complete": False,
            "semantic_complete": False,
            "continuation": {"available": False, "next_request": None},
            "send_enabled": False,
        }
    _assert_google_sheet_under_kniops(drive_service, target_id)
    metadata_payload_raw = (
        sheets_service.spreadsheets()
        .get(
            spreadsheetId=target_id,
            includeGridData=False,
            fields=(
                "spreadsheetId,properties(title,locale,timeZone),"
                "namedRanges(namedRangeId,name,range(sheetId,startRowIndex,endRowIndex,"
                "startColumnIndex,endColumnIndex)),sheets(properties(sheetId,title,"
                "gridProperties(rowCount,columnCount)))"
            ),
        )
        .execute()
    )
    metadata_payload = (
        dict(metadata_payload_raw) if isinstance(metadata_payload_raw, Mapping) else {}
    )
    fallback_resolved_range = str(metadata_payload.get("range") or "")
    source_identity = _google_sheet_source_identity(
        metadata_payload,
        spreadsheet_id=target_id,
    )
    public_source_identity = _google_sheet_public_source_identity(source_identity)
    if (
        expected_source_identity_sha256
        and source_identity["identity_sha256"] != expected_source_identity_sha256
    ):
        return {
            "status": "source_identity_changed",
            "operation": "read_table",
            "provider_read": True,
            "spreadsheet_id": target_id,
            "requested_range": requested_range,
            "source_identity": public_source_identity,
            "rows": [],
            "row_count": 0,
            "cells": [],
            "content_complete": False,
            "semantic_complete": False,
            "continuation": {"available": False, "next_request": None},
            "limitations": [
                "Spreadsheet identity metadata changed after the previous page; restart the read."
            ],
            "send_enabled": False,
        }
    resolution_range = requested_range
    resolution = _google_sheet_resolve_range(
        requested_range=resolution_range,
        requested_sheet_name=requested_sheet_name,
        source_identity=source_identity,
        metadata_payload=metadata_payload,
        fallback_resolved_range=fallback_resolved_range,
    )
    if resolution.get("status") != "success":
        return {
            "status": "blocked",
            "operation": "read_table",
            "provider_read": True,
            "spreadsheet_id": target_id,
            "requested_range": requested_range,
            "reason": str(resolution.get("status") or "range_resolution_failed"),
            "source_identity": public_source_identity,
            "rows": [],
            "row_count": 0,
            "cells": [],
            "content_complete": False,
            "semantic_complete": False,
            "continuation": {"available": False, "next_request": None},
            "send_enabled": False,
        }
    bounds = resolution["bounds"]
    public_source_identity = _google_sheet_public_source_identity(
        source_identity,
        resolution=resolution,
    )
    full_start_row = int(bounds["start_row"])
    full_end_row = int(bounds["end_row"])
    full_start_column = int(bounds["start_column"])
    full_end_column = int(bounds["end_column"])
    page_start_row = full_start_row + bounded_row_start
    page_start_column = full_start_column + bounded_column_start
    if page_start_row >= full_end_row or page_start_column >= full_end_column:
        return {
            "status": "out_of_range",
            "operation": "read_table",
            "provider_read": True,
            "spreadsheet_id": target_id,
            "requested_range": requested_range,
            "resolved_range": resolution["resolved_range"],
            "source_identity": public_source_identity,
            "rows": [],
            "row_count": 0,
            "cells": [],
            "content_complete": False,
            "semantic_complete": False,
            "continuation": {"available": False, "next_request": None},
            "send_enabled": False,
        }
    full_column_count = full_end_column - full_start_column
    candidate_cell_limit = (
        bounded_cells if representation == "source" else min(bounded_cells, 20)
    )
    band_column_count = min(full_column_count, bounded_columns, candidate_cell_limit)
    band_start_row = full_start_row + (
        bounded_row_band_start
        if bounded_row_band_start >= 0
        else bounded_row_start
    )
    band_row_count = bounded_row_band_rows or min(
        full_end_row - band_start_row,
        bounded_rows,
        max(1, candidate_cell_limit // band_column_count),
    )
    band_end_row = min(full_end_row, band_start_row + band_row_count)
    strip_start_column = full_start_column + (
        bounded_column_band_start
        if bounded_column_band_start >= 0
        else bounded_column_start
    )
    strip_column_count = bounded_column_band_columns or min(
        full_end_column - strip_start_column,
        band_column_count,
    )
    strip_end_column = min(
        full_end_column,
        strip_start_column + strip_column_count,
    )
    if not (
        band_start_row <= page_start_row < band_end_row
        and strip_start_column <= page_start_column < strip_end_column
    ):
        raise ValueError("Sheet continuation cursor is outside its declared row band or column strip.")
    page_column_count = strip_end_column - page_start_column
    page_row_count = min(
        band_end_row - page_start_row,
        bounded_row_chunk_rows or (band_end_row - page_start_row),
    )
    page_end_row = page_start_row + page_row_count
    page_end_column = page_start_column + page_column_count
    page_bounds = {
        "start_row": page_start_row,
        "end_row": page_end_row,
        "start_column": page_start_column,
        "end_column": page_end_column,
    }
    provider_read_range = _google_sheet_grid_range_a1(
        sheet_name=str(resolution["sheet_name"]),
        **page_bounds,
    )
    if representation == "source":
        grid_payload_raw = (
            sheets_service.spreadsheets()
            .get(
                spreadsheetId=target_id,
                ranges=[provider_read_range],
                includeGridData=True,
                fields=(
                    "spreadsheetId,properties(title,locale,timeZone),sheets("
                    "properties(sheetId,title),data(startRow,startColumn,rowData(values("
                    "userEnteredValue,effectiveValue,formattedValue,note,hyperlink,"
                    "effectiveFormat(numberFormat(type,pattern)),"
                    "userEnteredFormat(textFormat(link)),textFormatRuns(startIndex,format(link))"
                    "))))"
                ),
            )
            .execute()
        )
        grid_payload = dict(grid_payload_raw) if isinstance(grid_payload_raw, Mapping) else {}
        projection = _google_sheet_grid_projection(
            grid_payload,
            sheet_id=resolution.get("sheet_id"),
            page_bounds=page_bounds,
            note_start_char=bounded_note_start,
            max_note_chars=bounded_note_chars,
            link_start=bounded_link_start,
            max_links=bounded_links,
            value_start_char=bounded_value_start,
            max_value_chars=bounded_value_chars,
        )
    else:
        projection = None
    response_density_bounded = False
    while (
        projection is not None
        and (page_end_row - page_start_row) * (page_end_column - page_start_column) > 1
        and len(json.dumps(projection, ensure_ascii=True, sort_keys=True, default=str))
        > GOOGLE_SHEET_PROJECTION_CHAR_BUDGET
    ):
        projection_chars = len(
            json.dumps(projection, ensure_ascii=True, sort_keys=True, default=str)
        )
        current_cell_count = (page_end_row - page_start_row) * (
            page_end_column - page_start_column
        )
        safe_cell_count = max(
            1,
            min(
                current_cell_count - 1,
                int(
                    current_cell_count
                    * GOOGLE_SHEET_PROJECTION_CHAR_BUDGET
                    / projection_chars
                    * 0.8
                ),
            ),
        )
        if bounded_row_chunk_rows:
            safe_row_count = page_end_row - page_start_row
            safe_column_count = min(
                page_end_column - page_start_column,
                max(1, safe_cell_count // safe_row_count),
            )
        else:
            safe_column_count = min(
                page_end_column - page_start_column,
                safe_cell_count,
            )
            safe_row_count = min(
                page_end_row - page_start_row,
                max(1, safe_cell_count // safe_column_count),
            )
        page_end_row = page_start_row + safe_row_count
        page_end_column = page_start_column + safe_column_count
        page_bounds = {
            "start_row": page_start_row,
            "end_row": page_end_row,
            "start_column": page_start_column,
            "end_column": page_end_column,
        }
        projection = _google_sheet_grid_projection(
            grid_payload,
            sheet_id=resolution.get("sheet_id"),
            page_bounds=page_bounds,
            note_start_char=bounded_note_start,
            max_note_chars=bounded_note_chars,
            link_start=bounded_link_start,
            max_links=bounded_links,
            value_start_char=bounded_value_start,
            max_value_chars=bounded_value_chars,
        )
        response_density_bounded = True
    if projection is None:
        fallback_cell_limit = min(bounded_cells, 10)
        fallback_band_column_count = min(full_column_count, bounded_columns, fallback_cell_limit)
        fallback_column_count = min(
            strip_end_column - page_start_column,
            fallback_band_column_count,
        )
        fallback_row_count = min(
            band_end_row - page_start_row,
            bounded_row_chunk_rows
            or min(
                band_end_row - page_start_row,
                max(1, fallback_cell_limit // fallback_band_column_count),
            ),
        )
        page_end_row = page_start_row + fallback_row_count
        page_end_column = page_start_column + fallback_column_count
        page_bounds = {
            "start_row": page_start_row,
            "end_row": page_end_row,
            "start_column": page_start_column,
            "end_column": page_end_column,
        }
        provider_read_range = _google_sheet_grid_range_a1(
            sheet_name=str(resolution["sheet_name"]),
            **page_bounds,
        )
        projection = _google_sheet_values_projection(
            sheets_service,
            spreadsheet_id=target_id,
            read_range=provider_read_range,
            representation=representation,
            page_bounds=page_bounds,
            value_start_char=bounded_value_start,
            max_value_chars=bounded_value_chars,
        )
    read_range = _google_sheet_grid_range_a1(
        sheet_name=str(resolution["sheet_name"]),
        **page_bounds,
    )
    semantic_continuations: list[dict[str, Any]] = []
    for cell in projection.get("cells", []):
        if not isinstance(cell, dict):
            continue
        coordinate = str(cell.get("coordinate") or "")
        if not coordinate:
            continue
        cell_range = f"{_sheet_name_a1(str(resolution['sheet_name']))}!{coordinate}:{coordinate}"
        value_coverage = cell.get("value_coverage")
        if isinstance(value_coverage, dict) and value_coverage.get("has_more") is True:
            value_request = {
                "spreadsheet_id_or_url": target_id,
                "title": "",
                "folder_path": target_folder_path,
                "sheet_name": "",
                "range_a1": cell_range,
                "max_rows": 1,
                "max_columns": 1,
                "max_cells": 1,
                "row_band_start": 0,
                "row_band_rows": 1,
                "column_band_start": 0,
                "column_band_columns": 1,
                "row_chunk_rows": 1,
                "representation": representation,
                "note_start_char": 0,
                "max_note_chars": bounded_note_chars,
                "link_start": 0,
                "max_links": bounded_links,
                "value_start_char": bounded_value_start + bounded_value_chars,
                "max_value_chars": bounded_value_chars,
                "expected_source_identity_sha256": source_identity["identity_sha256"],
                "live": True,
            }
            value_coverage["next_request"] = value_request
            semantic_continuations.append(
                {
                    "kind": "cell_value",
                    "coordinate": coordinate,
                    "next_request": value_request,
                }
            )
        note_coverage = cell.get("note_coverage")
        if isinstance(note_coverage, dict) and note_coverage.get("has_more") is True:
            note_request = {
                "spreadsheet_id_or_url": target_id,
                "title": "",
                "folder_path": target_folder_path,
                "sheet_name": "",
                "range_a1": cell_range,
                "max_rows": 1,
                "max_columns": 1,
                "max_cells": 1,
                "row_band_start": 0,
                "row_band_rows": 1,
                "column_band_start": 0,
                "column_band_columns": 1,
                "row_chunk_rows": 1,
                "representation": "source",
                "note_start_char": int(note_coverage.get("end") or 0),
                "max_note_chars": bounded_note_chars,
                "link_start": 0,
                "max_links": bounded_links,
                "value_start_char": 0,
                "max_value_chars": bounded_value_chars,
                "expected_source_identity_sha256": source_identity["identity_sha256"],
                "live": True,
            }
            note_coverage["next_request"] = note_request
            semantic_continuations.append(
                {
                    "kind": "cell_note",
                    "coordinate": coordinate,
                    "next_request": note_request,
                }
            )
        link_coverage = cell.get("link_coverage")
        if isinstance(link_coverage, dict) and link_coverage.get("has_more") is True:
            link_request = {
                "spreadsheet_id_or_url": target_id,
                "title": "",
                "folder_path": target_folder_path,
                "sheet_name": "",
                "range_a1": cell_range,
                "max_rows": 1,
                "max_columns": 1,
                "max_cells": 1,
                "row_band_start": 0,
                "row_band_rows": 1,
                "column_band_start": 0,
                "column_band_columns": 1,
                "row_chunk_rows": 1,
                "representation": "source",
                "note_start_char": 0,
                "max_note_chars": bounded_note_chars,
                "link_start": int(link_coverage.get("end") or 0),
                "max_links": bounded_links,
                "value_start_char": 0,
                "max_value_chars": bounded_value_chars,
                "expected_source_identity_sha256": source_identity["identity_sha256"],
                "live": True,
            }
            link_coverage["next_request"] = link_request
            semantic_continuations.append(
                {
                    "kind": "cell_links",
                    "coordinate": coordinate,
                    "next_request": link_request,
                }
            )
    row_has_more = page_end_row < full_end_row
    column_has_more = page_end_column < full_end_column
    if page_end_column < strip_end_column:
        next_row_start = bounded_row_start
        next_column_start = bounded_column_start + (page_end_column - page_start_column)
        next_row_band_start = band_start_row - full_start_row
        next_row_band_rows = band_end_row - band_start_row
        next_column_band_start = strip_start_column - full_start_column
        next_column_band_columns = strip_end_column - strip_start_column
        next_row_chunk_rows = page_end_row - page_start_row
    elif page_end_row < band_end_row:
        next_row_start = bounded_row_start + (page_end_row - page_start_row)
        next_column_start = strip_start_column - full_start_column
        next_row_band_start = band_start_row - full_start_row
        next_row_band_rows = band_end_row - band_start_row
        next_column_band_start = strip_start_column - full_start_column
        next_column_band_columns = strip_end_column - strip_start_column
        next_row_chunk_rows = 0
    elif strip_end_column < full_end_column:
        next_row_start = band_start_row - full_start_row
        next_column_start = strip_end_column - full_start_column
        next_row_band_start = band_start_row - full_start_row
        next_row_band_rows = band_end_row - band_start_row
        next_column_band_start = strip_end_column - full_start_column
        next_column_band_columns = min(
            full_end_column - strip_end_column,
            band_column_count,
        )
        next_row_chunk_rows = 0
    elif band_end_row < full_end_row:
        next_row_start = band_end_row - full_start_row
        next_column_start = 0
        next_row_band_start = next_row_start
        next_row_band_rows = 0
        next_column_band_start = 0
        next_column_band_columns = 0
        next_row_chunk_rows = 0
    else:
        next_row_start = 0
        next_column_start = 0
        next_row_band_start = -1
        next_row_band_rows = 0
        next_column_band_start = -1
        next_column_band_columns = 0
        next_row_chunk_rows = 0
    has_more = any(
        (
            page_end_column < strip_end_column,
            page_end_row < band_end_row,
            strip_end_column < full_end_column,
            band_end_row < full_end_row,
        )
    )
    next_request = (
        {
            "spreadsheet_id_or_url": target_id,
            "title": "",
            "folder_path": target_folder_path,
            "sheet_name": requested_sheet_name,
            "range_a1": requested_range,
            "max_rows": bounded_rows,
            "max_columns": bounded_columns,
            "max_cells": bounded_cells,
            "row_start": next_row_start,
            "column_start": next_column_start,
            "row_band_start": next_row_band_start,
            "row_band_rows": next_row_band_rows,
            "column_band_start": next_column_band_start,
            "column_band_columns": next_column_band_columns,
            "row_chunk_rows": next_row_chunk_rows,
            "representation": representation,
            "note_start_char": 0,
            "max_note_chars": bounded_note_chars,
            "link_start": 0,
            "max_links": bounded_links,
            "value_start_char": 0,
            "max_value_chars": bounded_value_chars,
            "expected_source_identity_sha256": source_identity["identity_sha256"],
            "live": True,
        }
        if has_more
        else None
    )
    limitations = list(projection.get("limitations") or [])
    limitations.append(
        "Google Sheets exposes no cell-content revision token here; identity metadata is checked between pages, but cell snapshot isolation is not guaranteed."
    )
    if not source_identity.get("locale") or not source_identity.get("time_zone"):
        limitations.append(
            "Spreadsheet locale or time zone was unavailable; formatted dates and numbers must not be reinterpreted by locale guess."
        )
    if not bounds["row_end_bounded_by_source"] or not bounds["column_end_bounded_by_source"]:
        limitations.append(
            "Open or sheet-only range coverage follows provider grid allocation, which can include trailing empty capacity rather than the last populated cell."
        )
    rows = projection.get("rows") if isinstance(projection.get("rows"), list) else []
    semantic_complete = bool(
        projection.get("representation_coverage", {}).get("complete")
        if isinstance(projection.get("representation_coverage"), Mapping)
        else False
    )
    return {
        "status": "success",
        "operation": "read_table",
        "provider_read": True,
        "spreadsheet_id": target_id,
        "title": str(source_identity.get("title") or cleaned_title),
        "folder_path": target_folder_path,
        "sheet_id": resolution.get("sheet_id"),
        "sheet_name": resolution["sheet_name"],
        "named_range_name": resolution.get("named_range_name") or "",
        "named_range_id": resolution.get("named_range_id") or "",
        "requested_range": requested_range,
        "resolved_range": resolution["resolved_range"],
        "range": read_range,
        "provider_range": provider_read_range,
        "rows": rows,
        "row_count": len(rows),
        "cells": projection.get("cells", []),
        "missing_cells": projection.get("missing_cells", []),
        "representation": representation,
        "representations": projection.get("representations", {}),
        "representation_coverage": projection.get("representation_coverage", {}),
        "source_identity": public_source_identity,
        "coverage": {
            "unit": "grid_rectangle",
            "full": {
                "start_row": full_start_row,
                "end_row": full_end_row,
                "start_column": full_start_column,
                "end_column": full_end_column,
            },
            "window": page_bounds,
            "max_cells": bounded_cells,
            "response_density_bounded": response_density_bounded,
            "projection_char_budget": GOOGLE_SHEET_PROJECTION_CHAR_BUDGET,
            "row_band": {
                "start_row": band_start_row,
                "end_row": band_end_row,
            },
            "column_strip": {
                "start_column": strip_start_column,
                "end_column": strip_end_column,
            },
            "returned_rectangle_cells": (
                (page_end_row - page_start_row) * (page_end_column - page_start_column)
            ),
            "row_has_more": row_has_more,
            "column_has_more": column_has_more,
            "has_more": has_more,
            "complete": not has_more,
            "row_end_bounded_by_source": bounds["row_end_bounded_by_source"],
            "column_end_bounded_by_source": bounds["column_end_bounded_by_source"],
        },
        "content_complete": not has_more,
        "semantic_complete": semantic_complete,
        "semantic_continuations": semantic_continuations,
        "evidence_state": "present" if projection.get("cells") else "empty",
        "continuation": {
            "available": next_request is not None,
            "next_request": next_request,
        },
        "limitations": list(dict.fromkeys(limitations)),
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def google_sheet_read_table(
    spreadsheet_id_or_url: str = "",
    title: str = "",
    folder_path: str = "",
    sheet_name: str = "",
    range_a1: str = "",
    max_rows: int = 100,
    max_columns: int = 20,
    max_cells: int = GOOGLE_SHEET_READ_MAX_CELLS,
    row_start: int = 0,
    column_start: int = 0,
    row_band_start: int = -1,
    row_band_rows: int = 0,
    column_band_start: int = -1,
    column_band_columns: int = 0,
    row_chunk_rows: int = 0,
    representation: Literal["source", "formatted", "unformatted", "formula"] = "source",
    note_start_char: int = 0,
    max_note_chars: int = GOOGLE_SHEET_CELL_NOTE_PREVIEW_CHARS,
    link_start: int = 0,
    max_links: int = 10,
    value_start_char: int = 0,
    max_value_chars: int = GOOGLE_SHEET_CELL_VALUE_PREVIEW_CHARS,
    expected_source_identity_sha256: str = "",
    live: bool = False,
) -> str:
    """Read a bounded source-aware table or A1 range from an approved Sheet."""

    result = _google_workspace_read_call(
        "google_sheet_read_table",
        "read_table",
        lambda: google_sheet_read_table_impl(
            spreadsheet_id_or_url,
            title=title,
            folder_path=folder_path,
            sheet_name=sheet_name,
            range_a1=range_a1,
            max_rows=max_rows,
            max_columns=max_columns,
            max_cells=max_cells,
            row_start=row_start,
            column_start=column_start,
            row_band_start=row_band_start,
            row_band_rows=row_band_rows,
            column_band_start=column_band_start,
            column_band_columns=column_band_columns,
            row_chunk_rows=row_chunk_rows,
            representation=representation,
            note_start_char=note_start_char,
            max_note_chars=max_note_chars,
            link_start=link_start,
            max_links=max_links,
            value_start_char=value_start_char,
            max_value_chars=max_value_chars,
            expected_source_identity_sha256=expected_source_identity_sha256,
            live=live or _google_workspace_live_reads_default(),
        ),
    )
    return json.dumps(
        result,
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
        raise RuntimeError(
            "Google Sheet row deletion did not pass provider read-back verification."
        )
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
        if _airtable_field_write_blocked(field):
            if str(field.get("field_mode") or "") == "unknown":
                notes.append(f"`{clean_name}` has unknown writability and was not written.")
            else:
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


def _airtable_field_write_blocked(field: Mapping[str, Any]) -> bool:
    field_type = str(field.get("field_type") or "")
    field_mode = str(field.get("field_mode") or "").strip().lower()
    if field_mode in {"computed", "unknown"}:
        return True
    if "is_manual" in field and field.get("is_manual") is not True:
        return True
    return bool(field.get("is_computed")) or field_type in {
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
        if _airtable_field_write_blocked(field):
            if str(field.get("field_mode") or "") == "unknown":
                errors.append(f"Airtable field `{field_name}` has unknown writability.")
            else:
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
        value for name, value in verified_fields.items() if str(name).strip() == normalized_name
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
    raw_value = str(requested or "")
    choices = field.get("select_choices_exact") or field.get("select_choices", [])
    exact_matches = [str(choice) for choice in choices if str(choice) == raw_value]
    if len(exact_matches) == 1:
        return exact_matches[0]
    value = raw_value.strip().lower()
    normalized_matches = [str(choice) for choice in choices if str(choice).strip().lower() == value]
    if len(normalized_matches) == 1:
        return normalized_matches[0]
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
    elif normalized["estimated_tax_periods"]:
        period_number = _airtable_estimated_period_number(normalized["estimated_tax_periods"])
        if period_number is not None:
            normalized["estimated_tax_periods"] = str(period_number)
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
        if (
            extracted_value
            and model_value
            and not _receipt_values_equivalent(
                field_name,
                extracted_value,
                model_value,
            )
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
        return "1"
    if month <= 5:
        return "2"
    if month <= 8:
        return "3"
    return "4"


def _airtable_expense_period_context(
    table: str,
    *,
    base_alias: str,
    base_id: str,
    canonical_period: str,
) -> dict[str, Any]:
    """Verify one receipt period against the values already used by the base."""

    if canonical_period not in {"1", "2", "3", "4"}:
        return {
            "status": "blocked",
            "reason": "Receipt date did not resolve to a canonical tax period.",
            "canonical_value": canonical_period,
            "provider_read": False,
        }
    read_result = airtable_read_records_impl(
        table,
        base_alias=base_alias,
        base_id=base_id,
        max_records=500,
        fetch_all=True,
        live=True,
    )
    if read_result.get("status") != "success":
        return {
            "status": "blocked",
            "reason": "Business Expenses records could not be read before the write.",
            "canonical_value": canonical_period,
            "provider_read": False,
        }
    records = read_result.get("records")
    existing_values = sorted(
        {
            str(fields.get("Estimated Tax Periods") or "").strip()
            for record in records
            if isinstance(records, list) and isinstance(record, Mapping)
            for fields in [record.get("fields")]
            if isinstance(fields, Mapping)
            and str(fields.get("Estimated Tax Periods") or "").strip()
        }
    )
    if existing_values and canonical_period not in existing_values:
        return {
            "status": "blocked",
            "reason": (
                "The canonical receipt period is not an existing exact value in "
                "the Airtable expense table."
            ),
            "canonical_value": canonical_period,
            "existing_values": existing_values,
            "provider_read": True,
        }
    return {
        "status": "verified",
        "canonical_value": canonical_period,
        "existing_values": existing_values,
        "provider_read": True,
        "record_count": len(records) if isinstance(records, list) else 0,
    }


def _airtable_base_schema_url(base_id: str) -> str:
    return f"https://api.airtable.com/v0/meta/bases/{quote(base_id, safe='')}/tables"


def _airtable_table_url(table_name: str, *, base_id: str = "") -> str:
    if not base_id:
        base_id = context_env_value("AIRTABLE_BASE_ID").strip() or "app_dry_run"
    return f"https://api.airtable.com/v0/{base_id}/{quote(table_name, safe='')}"


def _airtable_record_provider_link(base_id: str, record_id: str) -> str:
    """Return Airtable's documented base/record URL for one exact record."""

    clean_base_id = str(base_id or "").strip()
    clean_record_id = str(record_id or "").strip()
    if not clean_base_id or not clean_record_id:
        return ""
    return f"https://airtable.com/{quote(clean_base_id, safe='')}/{quote(clean_record_id, safe='')}"


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
            f"{summary.base_name} reports {summary.table_count} provider tables; "
            f"{len(summary.tables)} are visible in this bounded preview. "
            f"Allowed finance/tax tables: {', '.join(summary.allowed_tables)}."
        ),
        object_id=summary.base_name,
        object_key="finance-tax-tracker",
        content={
            "base_id": summary.base_id,
            "base_name": summary.base_name,
            "source_snapshot_sha256": summary.source_snapshot_sha256,
            "allowed_tables": summary.allowed_tables,
            "tables_coverage": summary.tables_coverage,
            "tables": [
                {
                    "name": table.name,
                    "table_id": table.table_id,
                    "fields_coverage": table.fields_coverage,
                    "fields": [
                        {
                            "name": field.name,
                            "field_id": field.field_id,
                            "field_type": field.field_type,
                            "field_mode": field.field_mode,
                            "is_computed": field.is_computed,
                            "is_manual": field.is_manual,
                            "validity": field.validity,
                            "linked_table_id": field.linked_table_id,
                            "inverse_link_field_id": field.inverse_link_field_id,
                            "record_link_field_id": field.record_link_field_id,
                            "field_id_in_linked_table": field.field_id_in_linked_table,
                            "detail_read_required": field.detail_read_required,
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
    normalized = "\n".join(line.strip() for line in str(value or "").splitlines() if line.strip())
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
            text, text_truncated = _clean_slide_text(_powerpoint_xml_text(archive, name), max_chars)
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
    read_context = current_provider_read_context()
    if read_context is not None and read_context.plan.provider == "google_workspace":
        return read_context.service(
            "google_workspace.services",
            _build_google_workspace_services,
        )
    return _build_google_workspace_services()


def _start_google_workspace_read_attempt() -> None:
    read_context = current_provider_read_context()
    if read_context is None or read_context.plan.provider != "google_workspace":
        return
    if not read_context.try_start_attempt():
        raise ProviderReadContextError(
            "Google Workspace read exceeded its bounded call or deadline budget."
        )


def _build_google_workspace_services() -> dict[str, Any]:
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
    credentials = Credentials.from_authorized_user_file(str(token_path))
    return _LazyGoogleWorkspaceServices(
        credentials=credentials,
        build_service=build,
        token_path=token_path,
        refresh_request=GoogleAuthRequest,
    )


def _google_workspace_authorized_scopes(credentials: Any, token_path: Path) -> set[str]:
    """Read the token's granted scopes without requesting unrelated new scopes."""

    scopes = {
        str(scope).strip()
        for scope in (
            getattr(credentials, "granted_scopes", None)
            or getattr(credentials, "scopes", None)
            or []
        )
        if str(scope).strip()
    }
    if scopes:
        return scopes
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    raw_scopes = payload.get("scopes", []) if isinstance(payload, dict) else []
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.split()
    return {str(scope).strip() for scope in raw_scopes if str(scope).strip()}


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
    folder_id = _find_drive_folder_path(drive_service, folder_path)
    if not folder_id:
        raise RuntimeError(
            f"Allowed Google Drive folder '{folder_path}' does not exist; "
            "read-only validation will not create it."
        )
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
    base_folder_path = _google_docs_folder_path("")
    base_folder_id = _find_drive_folder_path(drive_service, base_folder_path)
    if not base_folder_id:
        raise RuntimeError(
            f"Allowed Google Drive folder '{base_folder_path}' does not exist; "
            "read-only validation will not create it."
        )
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
    if len(files) > 1:
        raise RuntimeError(
            f"Multiple Google Drive folders named '{name}' exist under the same parent; "
            "use an exact folder identity instead of selecting the first result."
        )
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
    mismatched_fields = sorted(
        str(key) for key in expected_fields if str(key) not in matched_fields
    )
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


def _google_doc_add_limitation(state: dict[str, Any], value: str) -> None:
    limitation = " ".join(str(value or "").split()).strip()
    if limitation and limitation not in state["limitations"]:
        state["limitations"].append(limitation)


def _google_doc_consume_node(state: dict[str, Any], kind: str) -> bool:
    if state["node_count"] >= GOOGLE_DOC_MAX_STRUCTURE_NODES:
        state["traversal_limited"] = True
        _google_doc_add_limitation(
            state,
            "Google Doc structural traversal stopped at the configured node limit.",
        )
        return False
    state["node_count"] += 1
    counts = state["structure_counts"]
    counts[kind] = int(counts.get(kind, 0)) + 1
    return True


def _google_doc_mark_unsupported(
    state: dict[str, Any],
    kind: str,
    limitation: str,
    *,
    visual: bool = False,
) -> None:
    unsupported = state["unsupported_elements"]
    unsupported[kind] = int(unsupported.get(kind, 0)) + 1
    if visual:
        state["unread_visual_count"] += 1
    _google_doc_add_limitation(state, limitation)


def _google_doc_mark_semantic_partial(state: dict[str, Any], limitation: str) -> None:
    state["semantic_partial"] = True
    _google_doc_add_limitation(state, limitation)


def _google_doc_link_destination(
    link: object,
    state: dict[str, Any],
) -> dict[str, str] | None:
    if not isinstance(link, Mapping):
        _google_doc_mark_semantic_partial(
            state,
            "A text link had malformed destination data.",
        )
        return None
    url = str(link.get("url") or "").strip()
    if url:
        return {"type": "url", "url": url}
    tab_id = str(link.get("tabId") or "").strip()
    if tab_id:
        return {"type": "tab", "tab_id": tab_id}
    for field, destination_type in (("bookmark", "bookmark"), ("heading", "heading")):
        value = link.get(field)
        if isinstance(value, Mapping):
            destination_id = str(value.get("id") or "").strip()
            destination_tab_id = str(value.get("tabId") or "").strip()
            if destination_id:
                return {
                    "type": destination_type,
                    "id": destination_id,
                    "tab_id": destination_tab_id,
                }
    legacy_bookmark = str(link.get("bookmarkId") or "").strip()
    if legacy_bookmark:
        return {"type": "bookmark", "id": legacy_bookmark, "tab_id": ""}
    legacy_heading = str(link.get("headingId") or "").strip()
    if legacy_heading:
        return {"type": "heading", "id": legacy_heading, "tab_id": ""}
    _google_doc_mark_semantic_partial(
        state,
        "A text link was present but its destination was unavailable or unsupported.",
    )
    return None


def _google_doc_string_ids(
    value: object,
    state: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _google_doc_mark_semantic_partial(
            state,
            f"A text run had malformed {label} data.",
        )
        return []
    return list(dict.fromkeys(item for item in value if item))


def _google_doc_add_semantic_annotation(
    state: dict[str, Any],
    annotation: dict[str, Any],
) -> None:
    serialized_length = len(json.dumps(annotation, ensure_ascii=True, sort_keys=True, default=str))
    if serialized_length > GOOGLE_DOC_SEMANTIC_ANNOTATION_MAX_CHARS:
        semantic_payload_sha256 = hashlib.sha256(
            json.dumps(
                annotation,
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        annotation = {
            "annotation_id": annotation["annotation_id"],
            "source_order": annotation["source_order"],
            "provider_range": annotation["provider_range"],
            "source_text": {
                "char_count": annotation["source_text"]["char_count"],
                "sha256": annotation["source_text"]["sha256"],
            },
            "change_state": annotation["change_state"],
            "strikethrough": annotation["strikethrough"],
            "link_present_but_omitted": annotation["link"] is not None,
            "suggested_insertion_present_but_omitted": bool(annotation["suggested_insertion_ids"]),
            "suggested_deletion_present_but_omitted": bool(annotation["suggested_deletion_ids"]),
            "suggested_style_changes_present_but_omitted": bool(
                annotation["suggested_style_changes"]
            ),
            "semantic_payload_omitted": True,
            "semantic_payload_sha256": semantic_payload_sha256,
        }
        serialized_length = len(
            json.dumps(annotation, ensure_ascii=True, sort_keys=True, default=str)
        )
        state["semantic_annotations_truncated"] = True
        _google_doc_mark_semantic_partial(
            state,
            "A Google Doc semantic annotation exceeded its bounded response size; "
            "its exact semantic payload was omitted and hashed.",
        )
    if len(state["semantic_annotations"]) >= GOOGLE_DOC_MAX_SEMANTIC_ANNOTATIONS:
        state["semantic_annotations_truncated"] = True
        _google_doc_mark_semantic_partial(
            state,
            "Google Doc semantic annotations exceeded their bounded response limit.",
        )
        return
    state["semantic_annotations"].append(annotation)
    state["semantic_annotation_chars"] += serialized_length


def _google_doc_text_run_semantics(
    text_run: Mapping[str, Any],
    element: Mapping[str, Any],
    state: dict[str, Any],
    content: str,
) -> None:
    style = text_run.get("textStyle", {})
    if style is None:
        style = {}
    if not isinstance(style, Mapping):
        _google_doc_mark_semantic_partial(state, "A text run had malformed textStyle data.")
        style = {}
    link = _google_doc_link_destination(style.get("link"), state) if "link" in style else None
    strikethrough = style.get("strikethrough") is True
    insertion_ids = _google_doc_string_ids(
        text_run.get("suggestedInsertionIds"),
        state,
        label="suggestedInsertionIds",
    )
    deletion_ids = _google_doc_string_ids(
        text_run.get("suggestedDeletionIds"),
        state,
        label="suggestedDeletionIds",
    )
    suggested_style_changes: list[dict[str, Any]] = []
    suggested_styles = text_run.get("suggestedTextStyleChanges")
    if suggested_styles is not None and not isinstance(suggested_styles, Mapping):
        _google_doc_mark_semantic_partial(
            state,
            "A text run had malformed suggestedTextStyleChanges data.",
        )
        suggested_styles = {}
    for suggestion_id, value in (suggested_styles or {}).items():
        if not isinstance(value, Mapping):
            _google_doc_mark_semantic_partial(
                state,
                "A suggested text-style change was malformed.",
            )
            continue
        suggestion_state = value.get("textStyleSuggestionState", {})
        suggested_style = value.get("textStyle", {})
        if not isinstance(suggestion_state, Mapping) or not isinstance(
            suggested_style,
            Mapping,
        ):
            _google_doc_mark_semantic_partial(
                state,
                "A suggested text-style change had malformed state or style data.",
            )
            continue
        change: dict[str, Any] = {"suggestion_id": str(suggestion_id)}
        if suggestion_state.get("strikethroughSuggested") is True:
            change["strikethrough"] = suggested_style.get("strikethrough") is True
        if suggestion_state.get("linkSuggested") is True:
            if "link" in suggested_style:
                change["link"] = _google_doc_link_destination(
                    suggested_style.get("link"),
                    state,
                )
            else:
                change["link"] = {"type": "removed"}
        if len(change) > 1:
            suggested_style_changes.append(change)

    if not (link or strikethrough or insertion_ids or deletion_ids or suggested_style_changes):
        return
    if insertion_ids and deletion_ids:
        change_state = "suggested_insertion_and_deletion"
    elif insertion_ids:
        change_state = "suggested_insertion"
    elif deletion_ids:
        change_state = "suggested_deletion"
    else:
        change_state = "base_content"
    start_index = element.get("startIndex")
    end_index = element.get("endIndex")
    provider_range = {
        "start_index": start_index if isinstance(start_index, int) else None,
        "end_index": end_index if isinstance(end_index, int) else None,
        "unit": "google_docs_utf16_code_units",
    }
    current_tab = state.get("current_tab", {})
    current_cell = state.get("current_table_cell", {})
    annotation = {
        "annotation_id": f"semantic-run-{state['semantic_run_count'] + 1}",
        "source_order": state["semantic_run_count"] + 1,
        "source_location": {
            "tab_id": str(current_tab.get("tab_id") or ""),
            "tab_position": str(current_tab.get("position") or ""),
            "segment": str(state.get("current_segment") or "body"),
            "table_index": current_cell.get("table_index"),
            "table_row": current_cell.get("row"),
            "table_cell_index": current_cell.get("cell_index"),
        },
        "provider_range": provider_range,
        "source_text": {
            "excerpt": content[:300],
            "char_count": len(content),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        },
        "change_state": change_state,
        "suggested_insertion_ids": insertion_ids,
        "suggested_deletion_ids": deletion_ids,
        "strikethrough": strikethrough,
        "link": link,
        "suggested_style_changes": suggested_style_changes,
    }
    state["semantic_run_count"] += 1
    _google_doc_add_semantic_annotation(state, annotation)


def _google_doc_paragraph_text(
    paragraph: Mapping[str, Any],
    state: dict[str, Any],
) -> tuple[str, bool, bool]:
    ordered_parts: list[str] = []
    has_source_text = False
    has_present_content = False
    elements = paragraph.get("elements", [])
    if not isinstance(elements, list):
        state["malformed_node_count"] += 1
        _google_doc_add_limitation(state, "A paragraph had malformed element data.")
        elements = []
    for element in elements:
        if not _google_doc_consume_node(state, "paragraph_elements"):
            ordered_parts.append("[[extraction annotation: structure limit reached]]")
            has_present_content = True
            break
        if not isinstance(element, Mapping):
            state["malformed_node_count"] += 1
            _google_doc_add_limitation(state, "A paragraph element was not an object.")
            ordered_parts.append("[[unsupported malformed paragraph element]]")
            has_present_content = True
            continue
        text_run = element.get("textRun")
        if isinstance(text_run, Mapping):
            content = text_run.get("content", "")
            if content is not None:
                resolved = str(content)
                ordered_parts.append(resolved)
                _google_doc_text_run_semantics(text_run, element, state, resolved)
                if resolved.strip():
                    has_source_text = True
                    has_present_content = True
            continue
        auto_text = element.get("autoText")
        if isinstance(auto_text, Mapping) and auto_text.get("content") is not None:
            resolved = str(auto_text.get("content") or "")
            ordered_parts.append(resolved)
            if resolved.strip():
                has_source_text = True
                has_present_content = True
            continue
        footnote = element.get("footnoteReference")
        if isinstance(footnote, Mapping):
            footnote_id = str(footnote.get("footnoteId") or "unknown")
            ordered_parts.append(f"[[footnote reference: {footnote_id}]]")
            has_present_content = True
            continue
        if "inlineObjectElement" in element:
            _google_doc_mark_unsupported(
                state,
                "inline_object",
                "Inline drawings or images are present but their visual content was not read.",
                visual=True,
            )
            ordered_parts.append("[[inline object: visual content not read]]")
            has_present_content = True
            continue
        if "equation" in element:
            _google_doc_mark_unsupported(
                state,
                "equation",
                "Equation content is present but was not interpreted.",
            )
            ordered_parts.append("[[equation: content not interpreted]]")
            has_present_content = True
            continue
        if "person" in element:
            _google_doc_mark_unsupported(
                state,
                "person",
                "A person smart chip is present but was not interpreted as source text.",
            )
            ordered_parts.append("[[person smart chip: content not interpreted]]")
            has_present_content = True
            continue
        if "richLink" in element:
            _google_doc_mark_unsupported(
                state,
                "rich_link",
                "A rich-link smart chip is present but was not interpreted as source text.",
            )
            ordered_parts.append("[[rich link: content not interpreted]]")
            has_present_content = True
            continue
        if "dateElement" in element:
            _google_doc_mark_unsupported(
                state,
                "date_element",
                "A date smart chip is present but was not interpreted as source text.",
            )
            ordered_parts.append("[[date smart chip: content not interpreted]]")
            has_present_content = True
            continue
        if "pageBreak" in element:
            ordered_parts.append("\n[[page break]]\n")
            has_present_content = True
            continue
        if "columnBreak" in element:
            ordered_parts.append("\n[[column break]]\n")
            has_present_content = True
            continue
        if "horizontalRule" in element:
            ordered_parts.append("\n[[horizontal rule]]\n")
            has_present_content = True
            continue
        keys = sorted(
            str(key) for key in element if key not in {"startIndex", "endIndex", "textStyle"}
        )
        if keys:
            key = keys[0]
            _google_doc_mark_unsupported(
                state,
                f"paragraph_element:{key}",
                f"Paragraph element '{key}' is not supported by this extractor.",
            )
            ordered_parts.append(f"[[unsupported paragraph element: {key}]]")
            has_present_content = True

    paragraph_text = "".join(ordered_parts).rstrip("\n")
    prefixes: list[str] = []
    style = paragraph.get("paragraphStyle")
    if isinstance(style, Mapping):
        named_style = str(style.get("namedStyleType") or "").strip()
        if named_style.startswith("HEADING_") or named_style in {"TITLE", "SUBTITLE"}:
            prefixes.append(f"[[{named_style.lower()}]]")
    bullet = paragraph.get("bullet")
    if isinstance(bullet, Mapping):
        nesting_level = bullet.get("nestingLevel", 0)
        prefixes.append(f"[[list item level={nesting_level}]]")
    if paragraph.get("suggestedParagraphStyleChanges"):
        _google_doc_mark_semantic_partial(
            state,
            "Suggested paragraph-style changes are present but were not interpreted.",
        )
    if paragraph.get("suggestedBulletChanges"):
        _google_doc_mark_semantic_partial(
            state,
            "Suggested bullet changes are present but were not interpreted.",
        )
    positioned_ids = paragraph.get("positionedObjectIds")
    if isinstance(positioned_ids, list) and positioned_ids:
        for _ in positioned_ids:
            _google_doc_mark_unsupported(
                state,
                "positioned_object",
                "Positioned drawings or images are present but their visual content was not read.",
                visual=True,
            )
        prefixes.append(f"[[{len(positioned_ids)} positioned object(s): visual content not read]]")
        has_present_content = True
    rendered = "\n".join([*prefixes, paragraph_text]).strip("\n")
    return rendered, has_source_text, has_present_content


def _google_doc_structural_text(
    content: object,
    state: dict[str, Any],
    *,
    depth: int = 0,
) -> tuple[list[str], bool, bool]:
    if depth > GOOGLE_DOC_MAX_STRUCTURE_DEPTH:
        state["traversal_limited"] = True
        _google_doc_add_limitation(
            state,
            "Google Doc structural traversal stopped at the configured nesting depth.",
        )
        return ["[[extraction annotation: nesting depth limit reached]]"], False, True
    if content is None:
        return [], False, False
    if not isinstance(content, list):
        state["malformed_node_count"] += 1
        _google_doc_add_limitation(state, "A structural content list was malformed.")
        return ["[[unsupported malformed structural content]]"], False, True

    parts: list[str] = []
    any_source_text = False
    any_present_content = False
    for item in content:
        if not _google_doc_consume_node(state, "structural_elements"):
            parts.append("[[extraction annotation: structure limit reached]]")
            any_present_content = True
            break
        if not isinstance(item, Mapping):
            state["malformed_node_count"] += 1
            _google_doc_add_limitation(state, "A structural element was not an object.")
            parts.append("[[unsupported malformed structural element]]")
            any_present_content = True
            continue
        paragraph = item.get("paragraph")
        if isinstance(paragraph, Mapping):
            state["structure_counts"]["paragraphs"] = (
                int(state["structure_counts"].get("paragraphs", 0)) + 1
            )
            rendered, has_source, has_present = _google_doc_paragraph_text(
                paragraph,
                state,
            )
            if rendered:
                parts.append(rendered)
            any_source_text = any_source_text or has_source
            any_present_content = any_present_content or has_present
            continue
        table = item.get("table")
        if isinstance(table, Mapping):
            state["structure_counts"]["tables"] = (
                int(state["structure_counts"].get("tables", 0)) + 1
            )
            table_index = int(state["structure_counts"]["tables"])
            any_present_content = True
            declared_rows = table.get("rows")
            declared_columns = table.get("columns")
            parts.append(
                "[[table "
                f"{table_index} start; declared_rows={declared_rows}; "
                f"declared_columns={declared_columns}]]"
            )
            rows = table.get("tableRows", [])
            if not isinstance(rows, list):
                state["malformed_node_count"] += 1
                _google_doc_add_limitation(state, "A table had malformed row data.")
                rows = []
            for row_index, row in enumerate(rows, start=1):
                if not _google_doc_consume_node(state, "table_rows"):
                    parts.append("[[extraction annotation: structure limit reached]]")
                    break
                parts.append(f"[[table {table_index} row {row_index}]]")
                if isinstance(row, Mapping) and (
                    row.get("suggestedInsertionIds") or row.get("suggestedDeletionIds")
                ):
                    _google_doc_mark_semantic_partial(
                        state,
                        "Suggested table-row insertion or deletion is present but was "
                        "not interpreted.",
                    )
                cells = row.get("tableCells", []) if isinstance(row, Mapping) else []
                if not isinstance(cells, list):
                    state["malformed_node_count"] += 1
                    _google_doc_add_limitation(state, "A table row had malformed cell data.")
                    cells = []
                for cell_index, cell in enumerate(cells, start=1):
                    if not _google_doc_consume_node(state, "table_cells"):
                        parts.append("[[extraction annotation: structure limit reached]]")
                        break
                    cell_content = cell.get("content", []) if isinstance(cell, Mapping) else []
                    if isinstance(cell, Mapping) and (
                        cell.get("suggestedInsertionIds") or cell.get("suggestedDeletionIds")
                    ):
                        _google_doc_mark_semantic_partial(
                            state,
                            "Suggested table-cell insertion or deletion is present but "
                            "was not interpreted.",
                        )
                    cell_style = cell.get("tableCellStyle", {}) if isinstance(cell, Mapping) else {}
                    row_span = 1
                    column_span = 1
                    if isinstance(cell_style, Mapping):
                        try:
                            row_span = max(1, int(cell_style.get("rowSpan") or 1))
                            column_span = max(1, int(cell_style.get("columnSpan") or 1))
                        except (TypeError, ValueError):
                            state["malformed_node_count"] += 1
                            _google_doc_add_limitation(
                                state,
                                "A table cell contained malformed rowSpan or columnSpan data.",
                            )
                    if row_span > 1 or column_span > 1:
                        state["merged_cell_count"] += 1
                        state["table_geometry_partial"] = True
                        _google_doc_add_limitation(
                            state,
                            "Merged-cell provider order and rowSpan/columnSpan are preserved, "
                            "but logical grid columns and header associations are not inferred.",
                        )
                    previous_cell = state.get("current_table_cell", {})
                    state["current_table_cell"] = {
                        "table_index": table_index,
                        "row": row_index,
                        "cell_index": cell_index,
                    }
                    cell_parts, cell_has_source, cell_has_present = _google_doc_structural_text(
                        cell_content,
                        state,
                        depth=depth + 1,
                    )
                    state["current_table_cell"] = previous_cell
                    parts.append(
                        f"[[table {table_index} cell row={row_index} "
                        f"cell_index={cell_index} row_span={row_span} "
                        f"column_span={column_span}]]"
                    )
                    if cell_parts:
                        parts.extend(cell_parts)
                    if not cell_has_present:
                        parts.append(
                            f"[[table {table_index} cell row={row_index} "
                            f"cell_index={cell_index}: blank]]"
                        )
                    any_source_text = any_source_text or cell_has_source
                    any_present_content = any_present_content or cell_has_present
            parts.append(f"[[table {table_index} end]]")
            continue
        table_of_contents = item.get("tableOfContents")
        if isinstance(table_of_contents, Mapping):
            state["structure_counts"]["table_of_contents"] = (
                int(state["structure_counts"].get("table_of_contents", 0)) + 1
            )
            toc_index = int(state["structure_counts"]["table_of_contents"])
            any_present_content = True
            parts.append(f"[[table of contents {toc_index} start]]")
            toc_parts, toc_has_source, toc_has_present = _google_doc_structural_text(
                table_of_contents.get("content", []),
                state,
                depth=depth + 1,
            )
            parts.extend(toc_parts)
            parts.append(f"[[table of contents {toc_index} end]]")
            any_source_text = any_source_text or toc_has_source
            any_present_content = any_present_content or toc_has_present
            continue
        if "sectionBreak" in item:
            state["structure_counts"]["section_breaks"] = (
                int(state["structure_counts"].get("section_breaks", 0)) + 1
            )
            parts.append("[[section break]]")
            any_present_content = True
            continue
        keys = sorted(str(key) for key in item if key not in {"startIndex", "endIndex"})
        key = keys[0] if keys else "unknown"
        _google_doc_mark_unsupported(
            state,
            f"structural_element:{key}",
            f"Structural element '{key}' is not supported by this extractor.",
        )
        parts.append(f"[[unsupported structural element: {key}]]")
        any_present_content = True
    return parts, any_source_text, any_present_content


def _google_doc_segment_text(
    segment: Mapping[str, Any],
    state: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    parts = [f"[[{label} start]]"]
    previous_segment = state.get("current_segment", "body")
    state["current_segment"] = label
    content_parts, _, _ = _google_doc_structural_text(
        segment.get("content", []),
        state,
    )
    state["current_segment"] = previous_segment
    parts.extend(content_parts)
    parts.append(f"[[{label} end]]")
    return parts


def _google_doc_document_tab_text(
    document_tab: Mapping[str, Any],
    state: dict[str, Any],
) -> list[str]:
    parts: list[str] = []
    unsupported_before = {
        "inline_object": int(state["unsupported_elements"].get("inline_object", 0)),
        "positioned_object": int(state["unsupported_elements"].get("positioned_object", 0)),
    }
    headers = document_tab.get("headers", {})
    if isinstance(headers, Mapping):
        for index, header in enumerate(headers.values(), start=1):
            if isinstance(header, Mapping):
                state["structure_counts"]["headers"] = (
                    int(state["structure_counts"].get("headers", 0)) + 1
                )
                parts.extend(_google_doc_segment_text(header, state, label=f"header {index}"))
    body = document_tab.get("body", {})
    if isinstance(body, Mapping):
        previous_segment = state.get("current_segment", "body")
        state["current_segment"] = "body"
        body_parts, _, _ = _google_doc_structural_text(body.get("content", []), state)
        state["current_segment"] = previous_segment
        parts.extend(body_parts)
    elif body is not None:
        state["malformed_node_count"] += 1
        _google_doc_add_limitation(state, "The Google Doc body was malformed.")
    footnotes = document_tab.get("footnotes", {})
    if isinstance(footnotes, Mapping):
        for footnote_id, footnote in footnotes.items():
            if isinstance(footnote, Mapping):
                state["structure_counts"]["footnotes"] = (
                    int(state["structure_counts"].get("footnotes", 0)) + 1
                )
                parts.extend(
                    _google_doc_segment_text(
                        footnote,
                        state,
                        label=f"footnote {str(footnote_id)}",
                    )
                )
    footers = document_tab.get("footers", {})
    if isinstance(footers, Mapping):
        for index, footer in enumerate(footers.values(), start=1):
            if isinstance(footer, Mapping):
                state["structure_counts"]["footers"] = (
                    int(state["structure_counts"].get("footers", 0)) + 1
                )
                parts.extend(_google_doc_segment_text(footer, state, label=f"footer {index}"))
    inline_objects = document_tab.get("inlineObjects", {})
    positioned_objects = document_tab.get("positionedObjects", {})
    for kind, values in (
        ("inline_object_map", inline_objects),
        ("positioned_object_map", positioned_objects),
    ):
        if isinstance(values, Mapping):
            referenced_kind = kind.removesuffix("_map")
            referenced_in_tab = max(
                0,
                int(state["unsupported_elements"].get(referenced_kind, 0))
                - unsupported_before[referenced_kind],
            )
            missing_count = max(
                0,
                len(values) - referenced_in_tab,
            )
            for _ in range(missing_count):
                _google_doc_mark_unsupported(
                    state,
                    kind,
                    "Google Doc visual objects are present but their visual content was not read.",
                    visual=True,
                )
    return parts


def _google_doc_tab_text(
    tab: Mapping[str, Any],
    state: dict[str, Any],
    *,
    depth: int,
    position: str,
) -> list[str]:
    if depth > GOOGLE_DOC_MAX_STRUCTURE_DEPTH:
        state["traversal_limited"] = True
        _google_doc_add_limitation(state, "Google Doc tab nesting exceeded the safe limit.")
        return ["[[extraction annotation: tab nesting depth limit reached]]"]
    if len(state["tabs"]) >= GOOGLE_DOC_MAX_TABS:
        state["traversal_limited"] = True
        _google_doc_add_limitation(state, "Google Doc tab traversal stopped at the safe limit.")
        return ["[[extraction annotation: tab limit reached]]"]
    properties = tab.get("tabProperties", {})
    title = str(properties.get("title") or "") if isinstance(properties, Mapping) else ""
    tab_id = str(properties.get("tabId") or "") if isinstance(properties, Mapping) else ""
    state["tabs"].append(
        {
            "position": position,
            "tab_id": tab_id,
            "title": title,
            "depth": depth,
        }
    )
    parts = [
        f"[[tab {position}; title={title or '(untitled)'}; "
        f"tab_id={tab_id or '(missing)'}; depth={depth}]]"
    ]
    previous_tab = state.get("current_tab", {})
    state["current_tab"] = {
        "position": position,
        "tab_id": tab_id,
        "title": title,
        "depth": depth,
    }
    document_tab = tab.get("documentTab")
    if isinstance(document_tab, Mapping):
        parts.extend(_google_doc_document_tab_text(document_tab, state))
    else:
        state["malformed_node_count"] += 1
        _google_doc_add_limitation(state, "A Google Doc tab had no readable documentTab.")
        parts.append("[[tab content unavailable]]")
    child_tabs = tab.get("childTabs", [])
    if isinstance(child_tabs, list):
        for index, child in enumerate(child_tabs, start=1):
            if isinstance(child, Mapping):
                parts.extend(
                    _google_doc_tab_text(
                        child,
                        state,
                        depth=depth + 1,
                        position=f"{position}.{index}",
                    )
                )
            else:
                state["malformed_node_count"] += 1
                _google_doc_add_limitation(state, "A child tab entry was malformed.")
    elif child_tabs is not None:
        state["malformed_node_count"] += 1
        _google_doc_add_limitation(state, "A childTabs collection was malformed.")
    state["current_tab"] = previous_tab
    return parts


def _google_doc_extraction(document: object) -> dict[str, Any]:
    """Extract bounded Docs text while keeping structure and gaps explicit."""

    state: dict[str, Any] = {
        "node_count": 0,
        "traversal_limited": False,
        "malformed_node_count": 0,
        "unread_visual_count": 0,
        "structure_counts": {},
        "unsupported_elements": {},
        "limitations": [],
        "tabs": [],
        "merged_cell_count": 0,
        "table_geometry_partial": False,
        "semantic_annotations": [],
        "semantic_annotation_chars": 0,
        "semantic_annotations_truncated": False,
        "semantic_partial": False,
        "semantic_run_count": 0,
        "current_tab": {},
        "current_segment": "body",
        "current_table_cell": {},
    }
    if not isinstance(document, Mapping):
        state["malformed_node_count"] = 1
        _google_doc_add_limitation(state, "The Google Docs API response was not an object.")
        return {
            "text": "[[unsupported malformed Google Doc response]]",
            "content_complete": False,
            "supported_text_complete": False,
            "visual_content_status": "unknown",
            "semantic_annotations": [],
            "semantic_coverage": {
                "suggestions_view_mode": "unavailable",
                "supported": False,
                "complete": False,
                "annotations_truncated": False,
            },
            "limitations": state["limitations"],
            "structure": {
                "schema": "keystone.google_doc_extraction.v1",
                "source_mode": "malformed",
                "node_count": 0,
                "counts": {},
                "unsupported_elements": {},
                "malformed_node_count": 1,
                "traversal_limited": False,
                "tabs": [],
            },
        }

    suggestions_view_mode = str(document.get("suggestionsViewMode") or "").strip()
    if suggestions_view_mode != "SUGGESTIONS_INLINE":
        _google_doc_mark_semantic_partial(
            state,
            "Google Doc suggestion semantics are incomplete because the provider response "
            "did not confirm SUGGESTIONS_INLINE mode.",
        )

    parts: list[str] = []
    tabs = document.get("tabs", [])
    if isinstance(tabs, list) and tabs:
        source_mode = "tabs"
        for index, tab in enumerate(tabs, start=1):
            if isinstance(tab, Mapping):
                parts.extend(_google_doc_tab_text(tab, state, depth=0, position=str(index)))
            else:
                state["malformed_node_count"] += 1
                _google_doc_add_limitation(state, "A top-level tab entry was malformed.")
    else:
        source_mode = "legacy_body"
        _google_doc_add_limitation(
            state,
            "The provider response did not include tab content; only legacy first-tab "
            "fields could be inspected.",
        )
        parts.extend(_google_doc_document_tab_text(document, state))

    text = "\n".join(part.strip("\n") for part in parts if str(part).strip("\n")).strip()
    unsupported = bool(state["unsupported_elements"])
    supported_text_complete = not (
        state["traversal_limited"]
        or state["malformed_node_count"]
        or unsupported
        or state["semantic_partial"]
    )
    content_complete = bool(
        source_mode == "tabs"
        and supported_text_complete
        and state["unread_visual_count"] == 0
        and not state["table_geometry_partial"]
    )
    return {
        "text": text,
        "content_complete": content_complete,
        "supported_text_complete": supported_text_complete,
        "visual_content_status": ("not_read" if state["unread_visual_count"] else "not_present"),
        "semantic_annotations": state["semantic_annotations"],
        "semantic_coverage": {
            "suggestions_view_mode": suggestions_view_mode or "unavailable",
            "supported": True,
            "complete": not state["semantic_partial"],
            "annotations_truncated": state["semantic_annotations_truncated"],
            "annotation_count": len(state["semantic_annotations"]),
        },
        "limitations": state["limitations"],
        "structure": {
            "schema": "keystone.google_doc_extraction.v1",
            "source_mode": source_mode,
            "include_tabs_content_requested": True,
            "node_count": state["node_count"],
            "counts": dict(sorted(state["structure_counts"].items())),
            "unsupported_elements": dict(sorted(state["unsupported_elements"].items())),
            "malformed_node_count": state["malformed_node_count"],
            "traversal_limited": state["traversal_limited"],
            "merged_cell_count": state["merged_cell_count"],
            "table_geometry_status": (
                "partial_merged_cells"
                if state["table_geometry_partial"]
                else "provider_order_unit_spans"
            ),
            "semantic_annotations_count": len(state["semantic_annotations"]),
            "semantic_annotations_truncated": state["semantic_annotations_truncated"],
            "tabs": state["tabs"],
        },
    }


def _google_doc_text(document: dict[str, Any]) -> str:
    """Compatibility text projection backed by the structured Docs extractor."""

    return str(_google_doc_extraction(document)["text"])


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
    owners = item.get("owners")
    if isinstance(owners, list):
        payload["owners"] = [
            {
                "display_name": str(owner.get("displayName", "")),
                "me": bool(owner.get("me")),
            }
            for owner in owners[:5]
            if isinstance(owner, dict) and str(owner.get("displayName", "")).strip()
        ]
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


def _append_doc_requests(docs_service: Any, document_id: str, body: str) -> list[dict[str, Any]]:
    document = docs_service.documents().get(documentId=document_id).execute()
    content = document.get("body", {}).get("content", []) if isinstance(document, dict) else []
    end_index = 1
    if content and isinstance(content[-1], dict):
        end_index = int(content[-1].get("endIndex", 1))
    existing_text = _google_doc_text(document)
    appended_text = ("\n" if existing_text else "") + body
    return [
        {
            "insertText": {
                "location": {"index": max(1, end_index - 1)},
                "text": appended_text,
            }
        }
    ]
