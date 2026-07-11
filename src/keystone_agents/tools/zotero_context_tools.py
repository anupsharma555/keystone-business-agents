"""Zotero context and guarded importer tools for specialist agents."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from keystone_agents.config import parse_bool
from keystone_agents.context_env import context_env_value
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.zotero_research import (
    build_zotero_article_research_brief,
    build_zotero_collection_research_brief,
)

ZOTERO_API_BASE_URL = "https://api.zotero.org"
ZOTERO_READ_CONTEXT_TOOL_NAMES: tuple[str, ...] = (
    "zotero_resolve_collection_context",
    "zotero_resolve_article_context",
    "zotero_read_api_metadata",
)
ZOTERO_IMPORT_TOOL_NAMES: tuple[str, ...] = (
    "zotero_import_article_with_backend",
)
ZOTERO_TEST_NOTE_TOOL_NAMES: tuple[str, ...] = (
    "zotero_write_test_note",
    "zotero_delete_test_note",
)
ZOTERO_TEST_LIBRARY_TOOL_NAMES: tuple[str, ...] = (
    "zotero_write_test_collection",
    "zotero_delete_test_collection",
    "zotero_write_test_item",
    "zotero_delete_test_item",
)
ZOTERO_CONTEXT_TOOL_NAMES: tuple[str, ...] = (
    *ZOTERO_READ_CONTEXT_TOOL_NAMES,
    *ZOTERO_IMPORT_TOOL_NAMES,
    *ZOTERO_TEST_NOTE_TOOL_NAMES,
    *ZOTERO_TEST_LIBRARY_TOOL_NAMES,
)
ZOTERO_TEST_NOTE_MARKER = "KBA_TEST_NOTE"
ZOTERO_TEST_COLLECTION_MARKER = "KBA_TEST_COLLECTION"
ZOTERO_TEST_ITEM_MARKER = "KBA_TEST_ITEM"
DEFAULT_ZOTERO_IMPORTER_SCRIPT = Path(
    os.getenv(
        "KEYSTONE_ZOTERO_IMPORTER_SCRIPT_DEFAULT",
        "../zotero-import/run_kni_zotero_importer.py",
    )
)


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _brief_payload(brief: Any, paragraphs: list[str], *, status: str = "success") -> dict[str, Any]:
    return {
        "status": status,
        "target_name": brief.target_name,
        "target_type": brief.target_type,
        "summary": brief.summary,
        "key_findings": brief.key_findings,
        "article_summaries": [item.model_dump(mode="json") for item in brief.article_summaries],
        "sources": [item.model_dump(mode="json") for item in brief.sources],
        "source_ids_used": brief.source_ids_used,
        "unknowns": brief.unknowns,
        "limitations": brief.limitations,
        "next_steps": brief.next_steps,
        "paragraphs": paragraphs,
        "send_enabled": False,
        "zotero_write_supported": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_resolve_collection_context(
    collection_hint: str,
    research_goal: str = "",
) -> str:
    """Resolve a local Zotero collection into source-backed context."""

    try:
        brief, paragraphs = build_zotero_collection_research_brief(
            collection_hint,
            research_goal=research_goal or f"Resolve Zotero collection context for {collection_hint}",
        )
    except Exception as exc:
        return _json_payload(
            {
                "status": "blocked",
                "reason": str(exc),
                "collection_hint": collection_hint,
                "send_enabled": False,
                "zotero_write_supported": False,
            }
        )
    return _json_payload(_brief_payload(brief, paragraphs))


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_resolve_article_context(
    query: str,
    research_goal: str = "",
) -> str:
    """Resolve a local Zotero item/article into source-backed context."""

    try:
        brief, paragraphs = build_zotero_article_research_brief(
            query,
            research_goal=research_goal or f"Resolve Zotero article context for {query}",
        )
    except Exception as exc:
        return _json_payload(
            {
                "status": "blocked",
                "reason": str(exc),
                "query": query,
                "send_enabled": False,
                "zotero_write_supported": False,
            }
        )
    return _json_payload(_brief_payload(brief, paragraphs))


def _zotero_api_path(
    *,
    library_type: str,
    library_id: str,
    collection_key: str,
    item_key: str,
    top_level_only: bool = False,
) -> str:
    clean_type = "groups" if str(library_type or "").strip().lower() == "group" else "users"
    if not library_id.strip():
        raise ValueError("library_id is required for live Zotero API reads.")
    base = f"/{clean_type}/{quote(library_id.strip(), safe='')}"
    if item_key.strip():
        return f"{base}/items/{quote(item_key.strip(), safe='')}"
    if collection_key.strip():
        suffix = "/items/top" if top_level_only else "/items"
        return f"{base}/collections/{quote(collection_key.strip(), safe='')}{suffix}"
    return f"{base}/items/top" if top_level_only else f"{base}/items"


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_read_api_metadata(
    library_id: str = "",
    library_type: str = "user",
    collection_key: str = "",
    item_key: str = "",
    query: str = "",
    limit: int = 25,
    sort: str = "",
    direction: str = "",
    top_level_only: bool = False,
    item_type: str = "",
    live: bool = False,
) -> str:
    """Read Zotero API metadata for libraries, collections, or items without mutation."""

    bounded_limit = min(max(int(limit or 25), 1), 100)
    params = {"limit": bounded_limit}
    if query.strip():
        params["q"] = query.strip()
    clean_item_type = item_type.strip()
    allowed_item_types = {
        "journalArticle",
        "preprint",
        "conferencePaper",
        "bookSection",
        "report",
        "thesis",
        "webpage",
    }
    if clean_item_type:
        if clean_item_type not in allowed_item_types:
            raise ValueError(
                "item_type must be one of: " + ", ".join(sorted(allowed_item_types))
            )
        params["itemType"] = clean_item_type
    clean_sort = sort.strip()
    allowed_sorts = {
        "dateAdded",
        "dateModified",
        "title",
        "creator",
        "date",
        "itemType",
    }
    if clean_sort:
        if clean_sort not in allowed_sorts:
            raise ValueError(
                "sort must be one of: " + ", ".join(sorted(allowed_sorts))
            )
        params["sort"] = clean_sort
    clean_direction = direction.strip().lower()
    if clean_direction:
        if clean_direction not in {"asc", "desc"}:
            raise ValueError("direction must be 'asc' or 'desc'.")
        if not clean_sort:
            raise ValueError("direction requires an explicit sort field.")
        params["direction"] = clean_direction
    planned_path = (
        _zotero_api_path(
            library_type=library_type,
            library_id=library_id or "configured_library_id",
            collection_key=collection_key,
            item_key=item_key,
            top_level_only=top_level_only,
        )
        if library_id.strip()
        else "/users/{library_id}/items"
    )
    if not live:
        return _json_payload(
            {
                "status": "dry-run",
                "api_base_url": ZOTERO_API_BASE_URL,
                "planned_path": planned_path,
                "params": params,
                "send_enabled": False,
                "zotero_write_supported": False,
                "required_live_env": ["ZOTERO_API_KEY", "ZOTERO_LIBRARY_ID"],
                "supported_reads": [
                    "library items",
                    "collection items",
                    "single item metadata",
                    "query search metadata",
                ],
            }
        )
    api_key = context_env_value("ZOTERO_API_KEY").strip()
    resolved_library_id = library_id.strip() or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key:
        raise RuntimeError("Live Zotero API reads require ZOTERO_API_KEY.")
    path = _zotero_api_path(
        library_type=library_type,
        library_id=resolved_library_id,
        collection_key=collection_key,
        item_key=item_key,
        top_level_only=top_level_only,
    )
    url = f"{ZOTERO_API_BASE_URL}{path}?{urlencode(params)}"
    request = Request(url, headers={"Zotero-API-Key": api_key, "Accept": "application/json"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _json_payload(
        {
            "status": "success",
            "api_base_url": ZOTERO_API_BASE_URL,
            "path": path,
            "params": params,
            "items": payload if isinstance(payload, list) else [payload],
            "send_enabled": False,
            "zotero_write_supported": False,
        }
    )


def zotero_write_test_note_impl(
    note_html: str,
    *,
    item_key: str = "",
    parent_item_key: str = "",
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> dict[str, Any]:
    """Create or update one provider-verified disposable Zotero note."""

    clean_note = str(note_html or "").strip()
    clean_operation = str(operation or "create").strip().lower()
    clean_item_key = str(item_key or "").strip()
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
    if not clean_note:
        raise ValueError("Zotero test-note writes require note_html.")
    if ZOTERO_TEST_NOTE_MARKER.lower() not in clean_note.lower():
        raise ValueError(f"Zotero test notes must contain {ZOTERO_TEST_NOTE_MARKER}.")
    if clean_operation == "update" and not clean_item_key:
        raise ValueError("Zotero test-note updates require an exact item_key.")

    planned_path = _zotero_items_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        item_key=clean_item_key if clean_operation == "update" else "",
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": clean_operation,
            "planned_path": planned_path,
            "item_key": clean_item_key,
            "parent_item_key": parent_item_key.strip(),
            "required_marker": ZOTERO_TEST_NOTE_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "zotero_test_note_write_supported": True,
        }

    api_key, resolved_library_id = _zotero_live_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    before_receipt: dict[str, Any] = {}
    if clean_operation == "update":
        before = _zotero_get_item(
            item_key=clean_item_key,
            library_id=resolved_library_id,
            library_type=library_type,
            api_key=api_key,
        )
        _require_marked_zotero_test_note(before, expected_key=clean_item_key)
        before_receipt = _zotero_note_receipt(before)
        version = _zotero_item_version(before)
        _zotero_http_request(
            "PATCH",
            _zotero_items_path(
                library_type=library_type,
                library_id=resolved_library_id,
                item_key=clean_item_key,
            ),
            api_key=api_key,
            payload={"note": clean_note},
            headers={"If-Unmodified-Since-Version": str(version)},
            expected_statuses=(204,),
        )
        resolved_item_key = clean_item_key
    else:
        note_payload: dict[str, Any] = {
            "itemType": "note",
            "note": clean_note,
            "tags": [{"tag": ZOTERO_TEST_NOTE_MARKER}],
            "collections": [],
            "relations": {},
        }
        if parent_item_key.strip():
            note_payload["parentItem"] = parent_item_key.strip()
        _status, created, _headers = _zotero_http_request(
            "POST",
            _zotero_items_path(
                library_type=library_type,
                library_id=resolved_library_id,
            ),
            api_key=api_key,
            payload=[note_payload],
            headers={"Zotero-Write-Token": uuid4().hex},
            expected_statuses=(200,),
        )
        resolved_item_key = _zotero_created_item_key(created)
        if not resolved_item_key:
            raise RuntimeError("Zotero test-note create returned no item key.")

    after = _zotero_get_item(
        item_key=resolved_item_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_note(after, expected_key=resolved_item_key)
    after_data = _zotero_item_data(after)
    note_match = _normalized_note(after_data.get("note")) == _normalized_note(clean_note)
    verification = {
        "status": "verified" if note_match else "verification_failed",
        "passed": note_match,
        "item_key_match": str(after_data.get("key") or "") == resolved_item_key,
        "item_type_note": after_data.get("itemType") == "note",
        "marker_present": _contains_zotero_test_note_marker(after_data),
        "note_match": note_match,
    }
    return {
        "status": "success" if verification["passed"] else "verification_failed",
        "operation": clean_operation,
        "item_key": resolved_item_key,
        "parent_item_key": str(after_data.get("parentItem") or ""),
        "required_marker": ZOTERO_TEST_NOTE_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": before_receipt,
        "after": _zotero_note_receipt(after),
        "verification": verification,
        "send_enabled": False,
        "zotero_test_note_write_supported": True,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_write_test_note(
    note_html: str,
    item_key: str = "",
    parent_item_key: str = "",
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> str:
    """Create or update one approved Zotero note containing KBA_TEST_NOTE."""

    return _json_payload(
        zotero_write_test_note_impl(
            note_html,
            item_key=item_key,
            parent_item_key=parent_item_key,
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            operation=operation,
            live=live,
        )
    )


def zotero_delete_test_note_impl(
    item_key: str,
    *,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one exact provider-verified disposable Zotero note."""

    clean_item_key = str(item_key or "").strip()
    if not clean_item_key:
        raise ValueError("Zotero test-note deletion requires an exact item_key.")
    planned_path = _zotero_items_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        item_key=clean_item_key,
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": "delete_test_note",
            "planned_path": planned_path,
            "item_key": clean_item_key,
            "required_marker": ZOTERO_TEST_NOTE_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "zotero_test_note_write_supported": True,
        }

    api_key, resolved_library_id = _zotero_live_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    before = _zotero_get_item(
        item_key=clean_item_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_note(before, expected_key=clean_item_key)
    version = _zotero_item_version(before)
    path = _zotero_items_path(
        library_type=library_type,
        library_id=resolved_library_id,
        item_key=clean_item_key,
    )
    _zotero_http_request(
        "DELETE",
        path,
        api_key=api_key,
        headers={"If-Unmodified-Since-Version": str(version)},
        expected_statuses=(204,),
    )
    after_status, _after, _headers = _zotero_http_request(
        "GET",
        path,
        api_key=api_key,
        expected_statuses=(200, 404),
    )
    passed = after_status == 404
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "delete_test_note",
        "item_key": clean_item_key,
        "required_marker": ZOTERO_TEST_NOTE_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": _zotero_note_receipt(before),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "item_absent_after": passed,
        },
        "send_enabled": False,
        "zotero_test_note_write_supported": True,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_delete_test_note(
    item_key: str,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved Zotero note only after proving KBA_TEST_NOTE."""

    return _json_payload(
        zotero_delete_test_note_impl(
            item_key,
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            live=live,
        )
    )


def zotero_write_test_collection_impl(
    name: str,
    *,
    collection_key: str = "",
    parent_collection_key: str = "",
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> dict[str, Any]:
    """Create or update one marked, provider-verified Zotero test collection."""

    clean_name = " ".join(str(name or "").split())
    clean_key = str(collection_key or "").strip()
    clean_operation = str(operation or "create").strip().lower()
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
    if ZOTERO_TEST_COLLECTION_MARKER.lower() not in clean_name.lower():
        raise ValueError(
            f"Zotero test collections must contain {ZOTERO_TEST_COLLECTION_MARKER}."
        )
    if clean_operation == "update" and not clean_key:
        raise ValueError("Zotero test-collection updates require an exact collection_key.")
    planned_path = _zotero_collections_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        collection_key=clean_key if clean_operation == "update" else "",
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": clean_operation,
            "planned_path": planned_path,
            "collection_key": clean_key,
            "name": clean_name,
            "parent_collection_key": parent_collection_key.strip(),
            "required_marker": ZOTERO_TEST_COLLECTION_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
        }
    api_key, resolved_library_id = _zotero_live_test_library_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    before_receipt: dict[str, Any] = {}
    if clean_operation == "update":
        before = _zotero_get_collection(
            collection_key=clean_key,
            library_id=resolved_library_id,
            library_type=library_type,
            api_key=api_key,
        )
        _require_marked_zotero_test_collection(before, expected_key=clean_key)
        before_receipt = _zotero_collection_receipt(before)
        version = _zotero_object_version(before)
        _zotero_http_request(
            "PATCH",
            _zotero_collections_path(
                library_type=library_type,
                library_id=resolved_library_id,
                collection_key=clean_key,
            ),
            api_key=api_key,
            payload={
                "name": clean_name,
                "parentCollection": parent_collection_key.strip() or False,
            },
            headers={"If-Unmodified-Since-Version": str(version)},
            expected_statuses=(204,),
        )
        resolved_key = clean_key
    else:
        _status, created, _headers = _zotero_http_request(
            "POST",
            _zotero_collections_path(
                library_type=library_type,
                library_id=resolved_library_id,
            ),
            api_key=api_key,
            payload=[
                {
                    "name": clean_name,
                    "parentCollection": parent_collection_key.strip() or False,
                }
            ],
            headers={"Zotero-Write-Token": uuid4().hex},
            expected_statuses=(200,),
        )
        resolved_key = _zotero_created_object_key(created)
        if not resolved_key:
            raise RuntimeError("Zotero test-collection create returned no collection key.")
    after = _zotero_get_collection(
        collection_key=resolved_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_collection(after, expected_key=resolved_key)
    data = _zotero_item_data(after)
    passed = str(data.get("name") or "") == clean_name
    return {
        "status": "success" if passed else "verification_failed",
        "operation": clean_operation,
        "collection_key": resolved_key,
        "name": str(data.get("name") or ""),
        "parent_collection_key": str(data.get("parentCollection") or ""),
        "required_marker": ZOTERO_TEST_COLLECTION_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": before_receipt,
        "after": _zotero_collection_receipt(after),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "collection_key_match": str(data.get("key") or "") == resolved_key,
            "marker_present": _contains_marker(data, ZOTERO_TEST_COLLECTION_MARKER),
            "name_match": passed,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_write_test_collection(
    name: str,
    collection_key: str = "",
    parent_collection_key: str = "",
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> str:
    """Create or update one approved collection containing KBA_TEST_COLLECTION."""

    return _json_payload(
        zotero_write_test_collection_impl(
            name,
            collection_key=collection_key,
            parent_collection_key=parent_collection_key,
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            operation=operation,
            live=live,
        )
    )


def zotero_write_test_item_impl(
    title: str,
    *,
    item_key: str = "",
    collection_key: str,
    url: str = "https://example.test/kba-zotero-validation",
    tags: list[str] | None = None,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> dict[str, Any]:
    """Create or update one marked webpage item in one exact test collection."""

    clean_title = " ".join(str(title or "").split())
    clean_item_key = str(item_key or "").strip()
    clean_collection_key = str(collection_key or "").strip()
    clean_operation = str(operation or "create").strip().lower()
    if clean_operation not in {"create", "update"}:
        raise ValueError("operation must be 'create' or 'update'.")
    if ZOTERO_TEST_ITEM_MARKER.lower() not in clean_title.lower():
        raise ValueError(f"Zotero test items must contain {ZOTERO_TEST_ITEM_MARKER}.")
    if not clean_collection_key:
        raise ValueError("Zotero test items require an exact collection_key.")
    if clean_operation == "update" and not clean_item_key:
        raise ValueError("Zotero test-item updates require an exact item_key.")
    clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    if ZOTERO_TEST_ITEM_MARKER not in clean_tags:
        clean_tags.append(ZOTERO_TEST_ITEM_MARKER)
    planned_path = _zotero_items_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        item_key=clean_item_key if clean_operation == "update" else "",
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": clean_operation,
            "planned_path": planned_path,
            "item_key": clean_item_key,
            "collection_key": clean_collection_key,
            "title": clean_title,
            "tags": clean_tags,
            "required_marker": ZOTERO_TEST_ITEM_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
        }
    api_key, resolved_library_id = _zotero_live_test_library_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    collection = _zotero_get_collection(
        collection_key=clean_collection_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_collection(collection, expected_key=clean_collection_key)
    before_receipt: dict[str, Any] = {}
    payload = {
        "title": clean_title,
        "url": str(url or "").strip(),
        "tags": [{"tag": tag} for tag in clean_tags],
        "collections": [clean_collection_key],
    }
    if clean_operation == "update":
        before = _zotero_get_item(
            item_key=clean_item_key,
            library_id=resolved_library_id,
            library_type=library_type,
            api_key=api_key,
        )
        _require_marked_zotero_test_item(before, expected_key=clean_item_key)
        before_receipt = _zotero_test_item_receipt(before)
        _zotero_http_request(
            "PATCH",
            _zotero_items_path(
                library_type=library_type,
                library_id=resolved_library_id,
                item_key=clean_item_key,
            ),
            api_key=api_key,
            payload=payload,
            headers={"If-Unmodified-Since-Version": str(_zotero_item_version(before))},
            expected_statuses=(204,),
        )
        resolved_key = clean_item_key
    else:
        _status, created, _headers = _zotero_http_request(
            "POST",
            _zotero_items_path(library_type=library_type, library_id=resolved_library_id),
            api_key=api_key,
            payload=[{"itemType": "webpage", "relations": {}, **payload}],
            headers={"Zotero-Write-Token": uuid4().hex},
            expected_statuses=(200,),
        )
        resolved_key = _zotero_created_object_key(created)
        if not resolved_key:
            raise RuntimeError("Zotero test-item create returned no item key.")
    after = _zotero_get_item(
        item_key=resolved_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_item(after, expected_key=resolved_key)
    data = _zotero_item_data(after)
    tag_values = _zotero_tag_values(data)
    collections = [str(value) for value in data.get("collections", [])]
    passed = bool(
        str(data.get("title") or "") == clean_title
        and clean_collection_key in collections
        and set(clean_tags) <= set(tag_values)
    )
    return {
        "status": "success" if passed else "verification_failed",
        "operation": clean_operation,
        "item_key": resolved_key,
        "collection_key": clean_collection_key,
        "title": str(data.get("title") or ""),
        "tags": tag_values,
        "required_marker": ZOTERO_TEST_ITEM_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": before_receipt,
        "after": _zotero_test_item_receipt(after),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "item_key_match": str(data.get("key") or "") == resolved_key,
            "marker_present": _contains_marker(data, ZOTERO_TEST_ITEM_MARKER),
            "title_match": str(data.get("title") or "") == clean_title,
            "collection_membership_match": clean_collection_key in collections,
            "tags_match": set(clean_tags) <= set(tag_values),
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_write_test_item(
    title: str,
    collection_key: str,
    item_key: str = "",
    url: str = "https://example.test/kba-zotero-validation",
    tags_json: str = "[]",
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    operation: str = "create",
    live: bool = False,
) -> str:
    """Create or update one marked Zotero webpage item with tags and collection membership."""

    parsed_tags = json.loads(tags_json or "[]")
    if not isinstance(parsed_tags, list):
        raise ValueError("tags_json must decode to a list of strings.")
    return _json_payload(
        zotero_write_test_item_impl(
            title,
            item_key=item_key,
            collection_key=collection_key,
            url=url,
            tags=[str(tag) for tag in parsed_tags],
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            operation=operation,
            live=live,
        )
    )


def zotero_delete_test_item_impl(
    item_key: str,
    *,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one exact marked Zotero test item and verify absence."""

    clean_key = str(item_key or "").strip()
    if not clean_key:
        raise ValueError("Zotero test-item deletion requires an exact item_key.")
    path = _zotero_items_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        item_key=clean_key,
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": "delete_test_item",
            "planned_path": path,
            "item_key": clean_key,
            "required_marker": ZOTERO_TEST_ITEM_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
        }
    api_key, resolved_library_id = _zotero_live_test_library_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    before = _zotero_get_item(
        item_key=clean_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_item(before, expected_key=clean_key)
    path = _zotero_items_path(
        library_type=library_type,
        library_id=resolved_library_id,
        item_key=clean_key,
    )
    _zotero_http_request(
        "DELETE",
        path,
        api_key=api_key,
        headers={"If-Unmodified-Since-Version": str(_zotero_item_version(before))},
        expected_statuses=(204,),
    )
    status, _payload, _headers = _zotero_http_request(
        "GET", path, api_key=api_key, expected_statuses=(200, 404)
    )
    passed = status == 404
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "delete_test_item",
        "item_key": clean_key,
        "required_marker": ZOTERO_TEST_ITEM_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": _zotero_test_item_receipt(before),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "item_absent_after": passed,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_delete_test_item(
    item_key: str,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved Zotero item only after proving KBA_TEST_ITEM."""

    return _json_payload(
        zotero_delete_test_item_impl(
            item_key,
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            live=live,
        )
    )


def zotero_delete_test_collection_impl(
    collection_key: str,
    *,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> dict[str, Any]:
    """Delete one exact marked empty Zotero test collection and verify absence."""

    clean_key = str(collection_key or "").strip()
    if not clean_key:
        raise ValueError("Zotero test-collection deletion requires an exact collection_key.")
    path = _zotero_collections_path(
        library_type=library_type,
        library_id=library_id or "configured_library_id",
        collection_key=clean_key,
    )
    if not live:
        return {
            "status": "dry-run",
            "operation": "delete_test_collection",
            "planned_path": path,
            "collection_key": clean_key,
            "required_marker": ZOTERO_TEST_COLLECTION_MARKER,
            "approval_reference": approval_reference.strip(),
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
        }
    api_key, resolved_library_id = _zotero_live_test_library_write_config(
        library_id=library_id,
        approval_reference=approval_reference,
    )
    before = _zotero_get_collection(
        collection_key=clean_key,
        library_id=resolved_library_id,
        library_type=library_type,
        api_key=api_key,
    )
    _require_marked_zotero_test_collection(before, expected_key=clean_key)
    item_status, items, _headers = _zotero_http_request(
        "GET",
        _zotero_collection_items_path(
            library_type=library_type,
            library_id=resolved_library_id,
            collection_key=clean_key,
        ),
        api_key=api_key,
        expected_statuses=(200,),
    )
    if item_status != 200 or not isinstance(items, list) or items:
        raise RuntimeError("Refusing to delete a non-empty Zotero test collection.")
    path = _zotero_collections_path(
        library_type=library_type,
        library_id=resolved_library_id,
        collection_key=clean_key,
    )
    _zotero_http_request(
        "DELETE",
        path,
        api_key=api_key,
        headers={"If-Unmodified-Since-Version": str(_zotero_object_version(before))},
        expected_statuses=(204,),
    )
    status, _payload, _headers = _zotero_http_request(
        "GET", path, api_key=api_key, expected_statuses=(200, 404)
    )
    passed = status == 404
    return {
        "status": "success" if passed else "verification_failed",
        "operation": "delete_test_collection",
        "collection_key": clean_key,
        "required_marker": ZOTERO_TEST_COLLECTION_MARKER,
        "approval_reference": approval_reference.strip(),
        "before": _zotero_collection_receipt(before),
        "verification": {
            "status": "verified" if passed else "verification_failed",
            "passed": passed,
            "collection_absent_after": passed,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_delete_test_collection(
    collection_key: str,
    library_id: str = "",
    library_type: str = "user",
    approval_reference: str = "",
    live: bool = False,
) -> str:
    """Delete one approved empty collection only after proving KBA_TEST_COLLECTION."""

    return _json_payload(
        zotero_delete_test_collection_impl(
            collection_key,
            library_id=library_id,
            library_type=library_type,
            approval_reference=approval_reference,
            live=live,
        )
    )


def _zotero_items_path(*, library_type: str, library_id: str, item_key: str = "") -> str:
    clean_type = "groups" if str(library_type or "").strip().lower() == "group" else "users"
    base = f"/{clean_type}/{quote(str(library_id).strip(), safe='')}/items"
    return f"{base}/{quote(item_key.strip(), safe='')}" if item_key.strip() else base


def _zotero_collections_path(
    *,
    library_type: str,
    library_id: str,
    collection_key: str = "",
) -> str:
    clean_type = "groups" if str(library_type or "").strip().lower() == "group" else "users"
    base = f"/{clean_type}/{quote(str(library_id).strip(), safe='')}/collections"
    return (
        f"{base}/{quote(collection_key.strip(), safe='')}"
        if collection_key.strip()
        else base
    )


def _zotero_collection_items_path(
    *,
    library_type: str,
    library_id: str,
    collection_key: str,
) -> str:
    return (
        _zotero_collections_path(
            library_type=library_type,
            library_id=library_id,
            collection_key=collection_key,
        )
        + "/items?limit=1"
    )


def _zotero_live_test_library_write_config(
    *,
    library_id: str,
    approval_reference: str,
) -> tuple[str, str]:
    if not approval_reference.strip():
        raise RuntimeError("Zotero test-library writes require a non-empty approval_reference.")
    if not parse_bool(os.getenv("KEYSTONE_ZOTERO_ALLOW_TEST_LIBRARY_WRITES")):
        raise RuntimeError(
            "Zotero test-library writes are disabled. Set "
            "KEYSTONE_ZOTERO_ALLOW_TEST_LIBRARY_WRITES=true for the approved test window."
        )
    api_key = context_env_value("ZOTERO_API_KEY").strip()
    resolved_library_id = library_id.strip() or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key or not resolved_library_id:
        raise RuntimeError("Live Zotero test-library writes require API key and library ID.")
    return api_key, resolved_library_id


def _zotero_get_collection(
    *,
    collection_key: str,
    library_id: str,
    library_type: str,
    api_key: str,
) -> dict[str, Any]:
    _status, payload, _headers = _zotero_http_request(
        "GET",
        _zotero_collections_path(
            library_type=library_type,
            library_id=library_id,
            collection_key=collection_key,
        ),
        api_key=api_key,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Zotero collection read returned an unexpected payload.")
    return payload


def _zotero_object_version(value: dict[str, Any]) -> int:
    data = _zotero_item_data(value)
    version = int(data.get("version") or value.get("version") or 0)
    if version <= 0:
        raise RuntimeError("Zotero object read returned no usable version.")
    return version


def _contains_marker(value: object, marker: str) -> bool:
    if isinstance(value, dict):
        return any(_contains_marker(item, marker) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_marker(item, marker) for item in value)
    return marker.lower() in str(value or "").lower()


def _require_marked_zotero_test_collection(
    collection: dict[str, Any],
    *,
    expected_key: str,
) -> None:
    data = _zotero_item_data(collection)
    if str(data.get("key") or "") != expected_key:
        raise RuntimeError("Zotero test-collection read-back did not match the exact key.")
    if not _contains_marker(data.get("name"), ZOTERO_TEST_COLLECTION_MARKER):
        raise RuntimeError("Resolved Zotero collection does not contain KBA_TEST_COLLECTION.")


def _zotero_tag_values(data: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for entry in data.get("tags", []):
        tag = entry.get("tag") if isinstance(entry, dict) else entry
        clean = str(tag or "").strip()
        if clean:
            values.append(clean)
    return values


def _require_marked_zotero_test_item(item: dict[str, Any], *, expected_key: str) -> None:
    data = _zotero_item_data(item)
    if str(data.get("key") or "") != expected_key:
        raise RuntimeError("Zotero test-item read-back did not match the exact item key.")
    if data.get("itemType") != "webpage":
        raise RuntimeError("Resolved Zotero test item is not a webpage.")
    if not (
        _contains_marker(data.get("title"), ZOTERO_TEST_ITEM_MARKER)
        and ZOTERO_TEST_ITEM_MARKER in _zotero_tag_values(data)
    ):
        raise RuntimeError("Resolved Zotero item does not contain KBA_TEST_ITEM markers.")


def _zotero_created_object_key(payload: object) -> str:
    return _zotero_created_item_key(payload)


def _zotero_collection_receipt(collection: dict[str, Any]) -> dict[str, Any]:
    data = _zotero_item_data(collection)
    return {
        "collection_key": str(data.get("key") or ""),
        "version": _zotero_object_version(collection),
        "name": str(data.get("name") or ""),
        "parent_collection_key": str(data.get("parentCollection") or ""),
        "marker_present": _contains_marker(data.get("name"), ZOTERO_TEST_COLLECTION_MARKER),
    }


def _zotero_test_item_receipt(item: dict[str, Any]) -> dict[str, Any]:
    data = _zotero_item_data(item)
    return {
        "item_key": str(data.get("key") or ""),
        "version": _zotero_item_version(item),
        "item_type": str(data.get("itemType") or ""),
        "title": str(data.get("title") or ""),
        "url": str(data.get("url") or ""),
        "tags": _zotero_tag_values(data),
        "collection_keys": [str(value) for value in data.get("collections", [])],
        "marker_present": _contains_marker(data, ZOTERO_TEST_ITEM_MARKER),
    }


def _zotero_live_write_config(*, library_id: str, approval_reference: str) -> tuple[str, str]:
    if not approval_reference.strip():
        raise RuntimeError("Zotero test-note writes require a non-empty approval_reference.")
    if not parse_bool(os.getenv("KEYSTONE_ZOTERO_ALLOW_TEST_NOTE_WRITES")):
        raise RuntimeError(
            "Zotero test-note writes are disabled. "
            "Set KEYSTONE_ZOTERO_ALLOW_TEST_NOTE_WRITES=true for the approved test window."
        )
    api_key = context_env_value("ZOTERO_API_KEY").strip()
    resolved_library_id = library_id.strip() or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key or not resolved_library_id:
        raise RuntimeError("Live Zotero test-note writes require API key and library ID.")
    return api_key, resolved_library_id


def _zotero_http_request(
    method: str,
    path: str,
    *,
    api_key: str,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    expected_statuses: tuple[int, ...] = (200,),
) -> tuple[int, object, dict[str, str]]:
    request_headers = {
        "Zotero-API-Key": api_key,
        "Zotero-API-Version": "3",
        "Accept": "application/json",
        **(headers or {}),
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        f"{ZOTERO_API_BASE_URL}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read()
            response_payload = _decode_zotero_response(raw)
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
        response_payload = _decode_zotero_response(raw)
        response_headers = dict(exc.headers.items()) if exc.headers else {}
    if status not in expected_statuses:
        raise RuntimeError(f"Zotero API {method} {path} returned HTTP {status}.")
    return status, response_payload, response_headers


def _decode_zotero_response(raw: bytes) -> object:
    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"non_json_response": text[:500]}


def _zotero_get_item(
    *,
    item_key: str,
    library_id: str,
    library_type: str,
    api_key: str,
) -> dict[str, Any]:
    _status, payload, _headers = _zotero_http_request(
        "GET",
        _zotero_items_path(
            library_type=library_type,
            library_id=library_id,
            item_key=item_key,
        ),
        api_key=api_key,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Zotero item read returned an unexpected payload.")
    return payload


def _zotero_item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data", item)
    if not isinstance(data, dict):
        raise RuntimeError("Zotero item data was not an object.")
    return data


def _zotero_item_version(item: dict[str, Any]) -> int:
    data = _zotero_item_data(item)
    version = int(data.get("version") or item.get("version") or 0)
    if version <= 0:
        raise RuntimeError("Zotero item read returned no usable version.")
    return version


def _contains_zotero_test_note_marker(value: object) -> bool:
    if isinstance(value, dict):
        return any(_contains_zotero_test_note_marker(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_zotero_test_note_marker(item) for item in value)
    return ZOTERO_TEST_NOTE_MARKER.lower() in str(value or "").lower()


def _require_marked_zotero_test_note(item: dict[str, Any], *, expected_key: str) -> None:
    data = _zotero_item_data(item)
    if str(data.get("key") or "") != expected_key:
        raise RuntimeError("Zotero test-note read-back did not match the exact item key.")
    if data.get("itemType") != "note":
        raise RuntimeError("Resolved Zotero item is not a note.")
    if not _contains_zotero_test_note_marker(data):
        raise RuntimeError("Resolved Zotero note does not contain KBA_TEST_NOTE.")


def _zotero_created_item_key(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for container_name in ("successful", "success"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        value = container.get("0") or next(iter(container.values()), "")
        if isinstance(value, dict):
            data = value.get("data", value)
            return str(data.get("key") or "") if isinstance(data, dict) else ""
        return str(value or "")
    return ""


def _normalized_note(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _zotero_note_receipt(item: dict[str, Any]) -> dict[str, Any]:
    data = _zotero_item_data(item)
    normalized_note = _normalized_note(data.get("note"))
    return {
        "item_key": str(data.get("key") or ""),
        "version": int(data.get("version") or item.get("version") or 0),
        "item_type": str(data.get("itemType") or ""),
        "parent_item_key": str(data.get("parentItem") or ""),
        "marker_present": _contains_zotero_test_note_marker(data),
        "note_sha256": hashlib.sha256(normalized_note.encode("utf-8")).hexdigest(),
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_import_article_with_backend(
    article_url: str,
    collection: str,
    tags: str = "",
    note: str = "",
    create_missing_collections: bool = False,
    approval_reference: str = "",
    write: bool = False,
    live: bool = False,
) -> str:
    """Preview or run the local KNI Zotero importer for one article URL."""

    clean_url = str(article_url or "").strip()
    clean_collection = str(collection or "").strip()
    if not clean_url:
        raise ValueError("article_url is required.")
    if not clean_collection:
        raise ValueError("collection is required.")
    script = Path(
        os.getenv("KEYSTONE_ZOTERO_IMPORTER_SCRIPT", str(DEFAULT_ZOTERO_IMPORTER_SCRIPT))
    ).expanduser()
    command = [
        sys.executable,
        str(script),
        "--article-url",
        clean_url,
        "--collection",
        clean_collection,
    ]
    if tags.strip():
        command.extend(["--tags", tags.strip()])
    if note.strip():
        command.extend(["--note", note.strip()])
    if create_missing_collections:
        command.append("--create-missing-collections")
    command.append("--write" if write else "--dry-run")
    if not live:
        return _json_payload(
            {
                "status": "dry-run",
                "command": command,
                "approval_reference": approval_reference.strip(),
                "write_requested": bool(write),
                "send_enabled": False,
                "zotero_write_supported": True,
                "notes": [
                    "Backend importer access is configured as a guarded direct-agent tool.",
                    "Set live=true to execute the importer subprocess.",
                    "Actual Zotero writes also require approval_reference and KEYSTONE_ZOTERO_IMPORTER_ALLOW_WRITES=true.",
                ],
            }
        )
    if not script.exists():
        raise FileNotFoundError(f"Zotero importer script not found: {script}")
    if write:
        if not approval_reference.strip():
            raise RuntimeError("Zotero importer writes require a non-empty approval_reference.")
        if os.getenv("KEYSTONE_ZOTERO_IMPORTER_ALLOW_WRITES", "").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise RuntimeError(
                "Zotero importer writes are disabled. Set "
                "KEYSTONE_ZOTERO_IMPORTER_ALLOW_WRITES=true after approval."
            )
    result = subprocess.run(
        command,
        cwd=str(script.parent),
        text=True,
        capture_output=True,
        timeout=180,
    )
    return _json_payload(
        {
            "status": "success" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "command": command,
            "stdout": result.stdout[-6000:],
            "stderr": result.stderr[-2000:],
            "approval_reference": approval_reference.strip(),
            "write_requested": bool(write),
            "send_enabled": False,
            "zotero_write_supported": True,
        }
    )
