"""Internal article, Airtable, and Google Workspace tools for Chief of Staff."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from keystone_agents.config import parse_bool
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
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
from keystone_agents.tools.website_extraction_tool import extract_website_content

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
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
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
    "google_drive_list_folder",
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


def google_workspace_tools() -> list[Any]:
    """Return scoped Google Drive, Docs, and Sheets tools for agent builders."""

    return [
        google_doc_read,
        google_doc_write,
        google_drive_list_folder,
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
    """Return true only when the operator asks to open or read full linked content."""

    lowered = str(text or "").lower()
    if not any(marker in lowered for marker in ("article", "link", "url", "page", "source")):
        return False
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
    return any(marker in lowered for marker in explicit_markers)


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
    if not live:
        return {
            "status": "dry-run",
            "url": normalized_url,
            "max_chars": bounded_chars,
            "planned_provider": os.getenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura"),
            "send_enabled": False,
        }
    result = extract_website_content(
        normalized_url,
        company_name="article",
        provider=os.getenv("KEYSTONE_WEBSITE_EXTRACTOR") or "trafilatura",
        live=True,
    )
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
    """Extract full linked article text only for explicit full-read requests."""

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
            live=live,
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

    config = _airtable_base_config(base_alias=base_alias, base_id=base_id)
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
            live=live,
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
    live: bool = False,
) -> dict[str, Any]:
    """Create or update Airtable records behind explicit approval and env gates."""

    fields = _json_object(fields_json, "fields_json")
    config = _airtable_base_config(base_alias=base_alias, base_id=base_id)
    table_name = _airtable_table(table, config=config)
    clean_record_id = record_id.strip()
    clean_operation = str(operation or "create").strip().lower()
    clean_match_filter = match_filter_formula.strip()
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
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
        "params": {},
        "payload": {"fields": fields},
    }
    dry_run = not live or parse_bool(os.getenv("AIRTABLE_WRITE_DRY_RUN", "true"))
    if dry_run:
        return {
            "status": "dry-run",
            "request": _safe_request_preview(request),
            "approval_reference": approval_reference.strip(),
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
    return {
        "status": "success",
        "table": table_name,
        "record_id": written_record_id,
        "record": payload,
        "verified_record": verified_records[0] if verified_records else {},
        "approval_reference": approval_reference.strip(),
        "send_enabled": False,
        "audit_notes": [
            "Airtable live write completed.",
            (
                "Read-after-write verification returned the updated record."
                if verified_records
                else "Read-after-write verification returned no record."
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
        "document_id": document_id,
        "title": title,
        "text": text,
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
            live=live,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
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
        "folder_id": folder_id,
        "folder_path": target_folder_path,
        "items": [_safe_drive_item(item) for item in files if isinstance(item, dict)],
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
            live=live,
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
        google_sheet_list_impl(folder_path, max_items=max_items, live=live),
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
    folder_id = _ensure_drive_folder_path(drive_service, target_folder_path)
    if folder_id:
        _move_drive_file_to_folder(drive_service, spreadsheet_id, folder_id)
    return {
        "status": "success",
        "spreadsheet_id": spreadsheet_id,
        "title": cleaned_title,
        "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        "folder_path": target_folder_path,
        "tabs": tabs,
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
            "spreadsheet_id": spreadsheet_id,
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "sheet_name": cleaned_sheet_name,
            "range": read_range,
            "rows": [],
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
            "title": cleaned_title,
            "folder_path": target_folder_path,
            "rows": [],
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
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "folder_path": target_folder_path,
        "sheet_name": cleaned_sheet_name,
        "range": read_range,
        "rows": rows[:bounded_rows],
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
            live=live,
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
    return {
        "status": "success",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "row_count": len(rows),
        "updated_range": response.get("updates", {}).get("updatedRange", "")
        if isinstance(response, dict)
        else "",
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
    return {
        "status": "success",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "row_number": matched_row_number,
        "key_column": key_column.strip(),
        "key_value": key_value.strip(),
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
    target_row_index = row_index
    if not target_row_index:
        table = _read_sheet_values(sheets_service, target_id, cleaned_sheet_name)
        _, target_row_index, _ = _match_sheet_row(table, key_column.strip(), key_value.strip())
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
    return {
        "status": "success",
        "spreadsheet_id": target_id,
        "title": cleaned_title,
        "sheet_name": cleaned_sheet_name,
        "deleted_row_index": target_row_index,
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
    return {
        "status": "success",
        "spreadsheet_id": spreadsheet_id,
        "name": metadata.get("name", "") if isinstance(metadata, dict) else "",
        "trashed": bool(metadata.get("trashed")) if isinstance(metadata, dict) else True,
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


def _airtable_csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def _airtable_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
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
    prefix = _airtable_alias_prefix(base_alias)
    resolved_base_id = (
        base_id.strip()
        or (os.getenv(f"{prefix}_BASE_ID", "").strip() if prefix else "")
        or os.getenv("AIRTABLE_BASE_ID", "").strip()
    )
    resolved_base_name = (
        base_name.strip()
        or (os.getenv(f"{prefix}_BASE_NAME", "").strip() if prefix else "")
        or os.getenv("AIRTABLE_BASE_NAME", "").strip()
    )
    access_token = (os.getenv(f"{prefix}_ACCESS_TOKEN", "").strip() if prefix else "") or os.getenv(
        "AIRTABLE_ACCESS_TOKEN", ""
    ).strip()
    default_table = (
        os.getenv(f"{prefix}_DEFAULT_TABLE", "").strip() if prefix else ""
    ) or os.getenv("AIRTABLE_DEFAULT_TABLE", "").strip()
    default_view = (os.getenv(f"{prefix}_DEFAULT_VIEW", "").strip() if prefix else "") or os.getenv(
        "AIRTABLE_DEFAULT_VIEW", ""
    ).strip()
    return {
        "base_alias": str(base_alias or "").strip(),
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


def _airtable_base_schema_url(base_id: str) -> str:
    return f"https://api.airtable.com/v0/meta/bases/{quote(base_id, safe='')}/tables"


def _airtable_table_url(table_name: str, *, base_id: str = "") -> str:
    if not base_id:
        base_id = os.getenv("AIRTABLE_BASE_ID", "").strip() or "app_dry_run"
    return f"https://api.airtable.com/v0/{base_id}/{quote(table_name, safe='')}"


def _require_airtable_credentials(*, base_id: str = "", access_token: str = "") -> None:
    if not (base_id or os.getenv("AIRTABLE_BASE_ID", "").strip()):
        raise RuntimeError("Missing Airtable configuration: AIRTABLE_BASE_ID.")
    if not (access_token or os.getenv("AIRTABLE_ACCESS_TOKEN", "").strip()):
        raise RuntimeError("Missing Airtable configuration: AIRTABLE_ACCESS_TOKEN.")


def _airtable_send(request: dict[str, Any], *, access_token: str = "") -> dict[str, Any]:
    query = urlencode(request.get("params", {}))
    request_url = request["url"] if not query else f"{request['url']}?{query}"
    payload = request.get("payload")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    outbound = Request(request_url, data=body, method=str(request["method"]))
    token = access_token or os.environ["AIRTABLE_ACCESS_TOKEN"]
    outbound.add_header("Authorization", f"Bearer {token}")
    outbound.add_header("Content-Type", "application/json")
    outbound.add_header("Accept", "application/json")
    timeout_seconds = int(os.getenv("AIRTABLE_REQUEST_TIMEOUT_SECONDS", "20"))
    with urlopen(outbound, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


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


def _google_workspace_services() -> dict[str, Any]:
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Install google-api-python-client and google-auth to use live Google Workspace tools."
        ) from exc

    token_path = Path(
        os.getenv("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH", ".local/google-workspace-oauth-token.json")
    ).expanduser()
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
    }


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
        os.getenv("KEYSTONE_GOOGLE_DOCS_FOLDER", "").strip()
        or os.getenv("GOOGLE_DRIVE_KNI_OPS_FOLDER", "").strip()
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
    expected = os.getenv("GOOGLE_DRIVE_ACCOUNT", "").strip().lower()
    if not expected or not parse_bool(os.getenv("GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH", "true")):
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


def _safe_drive_item(item: dict[str, Any]) -> dict[str, str]:
    mime_type = str(item.get("mimeType", ""))
    if mime_type == GOOGLE_FOLDER_MIME_TYPE:
        item_type = "folder"
    elif mime_type == "application/vnd.google-apps.document":
        item_type = "google_doc"
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        item_type = "google_sheet"
    else:
        item_type = "file"
    return {
        "id": str(item.get("id", "")),
        "name": str(item.get("name", "")),
        "mime_type": mime_type,
        "type": item_type,
        "url": str(item.get("webViewLink", "")),
        "modified_time": str(item.get("modifiedTime", "")),
    }


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
