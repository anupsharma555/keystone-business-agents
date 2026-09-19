"""Zotero context and guarded importer tools for specialist agents."""

from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from keystone_agents.config import parse_bool
from keystone_agents.context_env import context_env_value
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.receipts.journal import durable_provider_tool, record_provider_observation
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.schemas.zotero_reads import (
    ZoteroChildrenContinuation,
    ZoteroMetadataContinuation,
    ZoteroMetadataScope,
    ZoteroNoteContinuation,
    ZoteroPdfContinuation,
)
from keystone_agents.sdk import function_tool
from keystone_agents.source_specific_enrichment import enrich_source_reference
from keystone_agents.zotero_research import (
    build_zotero_article_research_brief,
    build_zotero_collection_research_brief,
    list_zotero_cached_item_metadata,
)

ZOTERO_API_BASE_URL = "https://api.zotero.org"
ZOTERO_READ_CONTEXT_TOOL_NAMES: tuple[str, ...] = (
    "zotero_list_cached_items",
    "zotero_resolve_collection_context",
    "zotero_resolve_article_context",
    "zotero_read_api_metadata",
    "zotero_read_item_children",
    "zotero_read_pdf_attachment_text",
)
ZOTERO_IMPORT_TOOL_NAMES: tuple[str, ...] = (
    "zotero_import_article_with_backend",
)
ZOTERO_TEST_NOTE_TOOL_NAMES: tuple[str, ...] = (
    "zotero_write_test_note",
    "zotero_delete_test_note",
    "zotero_test_note_lifecycle",
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
ZOTERO_OPERATOR_APPROVAL_ENV = "KEYSTONE_ZOTERO_OPERATOR_APPROVAL_REFERENCE"
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


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_list_cached_items(
    limit: int = 10,
    item_type: str = "",
) -> str:
    """List a bounded set of readable local Zotero cache items without mutation."""

    try:
        items = list_zotero_cached_item_metadata(limit=limit, item_type=item_type)
    except Exception as exc:
        return _json_payload(
            {
                "status": "blocked",
                "reason": str(exc),
                "items": [],
                "send_enabled": False,
                "zotero_write_supported": False,
            }
        )
    return _json_payload(
        {
            "status": "success" if items else "not_found",
            "items": items,
            "item_count": len(items),
            "identity_fingerprints": identity_fingerprints(
                (
                    item.get("item_key")
                    or item.get("key")
                    or (
                        item.get("data", {}).get("key")
                        if isinstance(item.get("data"), Mapping)
                        else ""
                    )
                )
                for item in items
                if isinstance(item, Mapping)
            ),
            "item_type_filter": item_type.strip(),
            "source": "local_zotero_cache",
            "send_enabled": False,
            "zotero_write_supported": False,
        }
    )


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


class _ZoteroAPIItems(list):
    """Keep transport coverage without changing the JSON-list read contract."""

    def __init__(self, items: list[Any], headers: Mapping[str, Any]) -> None:
        super().__init__(items)
        self.read_headers = {str(k).lower(): str(v) for k, v in headers.items()}


def _read_zotero_api_json(path: str, *, api_key: str, params: dict[str, Any]) -> Any:
    query = f"?{urlencode(params)}" if params else ""
    request = Request(
        f"{ZOTERO_API_BASE_URL}{path}{query}",
        headers={
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, list):
            return _ZoteroAPIItems(payload, getattr(response, "headers", {}))
        return payload


def _zotero_page_coverage(
    payload: Any,
    *,
    path: str,
    start: int,
    limit: int,
    pages_read: int,
    expected_version: int | None = None,
) -> dict[str, Any]:
    headers = getattr(payload, "read_headers", {})
    total = headers.get("total-results", "")
    total = int(total) if str(total).isdigit() else None
    version = headers.get("last-modified-version", "")
    version = int(version) if str(version).isdigit() else None
    if expected_version is not None and version != expected_version:
        raise RuntimeError("Zotero library version changed or is unavailable; restart the read.")
    count = len(payload) if isinstance(payload, list) else 1
    next_start = None
    limitations: list[str] = []
    for link in headers.get("link", "").split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not match:
            continue
        target = urlsplit(match.group(1))
        # Never follow a provider-supplied URL or broaden its query: only its
        # bounded offset is used with the original typed scope.
        if target.scheme != "https" or target.netloc != "api.zotero.org" or target.path != path:
            raise RuntimeError("Zotero next-page link does not match the selected source.")
        value = parse_qs(target.query).get("start", [""])[0]
        if not value.isdigit() or int(value) <= start:
            raise RuntimeError("Zotero next-page offset is invalid.")
        if int(value) != start + count:
            raise RuntimeError("Zotero next-page offset would skip or overlap source records.")
        next_start = int(value)
    if next_start is None and total is not None and start + count < total and count:
        next_start = start + count
    complete = not isinstance(payload, list) or (
        total is not None and start + count >= total and next_start is None
    )
    if total is None and isinstance(payload, list):
        limitations.append("Provider total is unavailable; absence is limited to this page.")
    if next_start is not None and (pages_read >= 10 or next_start >= 1000):
        limitations.append("The continuation budget of 10 pages / 1000 records was reached.")
    if next_start is not None and version is None:
        limitations.append("A source version is required before continuing this read.")
    return {
        "scope": "single_item" if not isinstance(payload, list) else "provider_page",
        "start": start,
        "page_size_limit": limit,
        "provider_items_on_page": count,
        "total_results": total,
        "library_version": version,
        "pages_read": pages_read,
        "complete": complete,
        "has_more": next_start is not None,
        "next_start": next_start,
        "continuation_supported": bool(
            next_start is not None and next_start < 1000 and pages_read < 10 and version is not None
        ),
        "limitations": limitations,
    }


def _read_zotero_api_bytes(path: str, *, api_key: str) -> tuple[bytes, str]:
    request = Request(
        f"{ZOTERO_API_BASE_URL}{path}",
        headers={
            "Zotero-API-Key": api_key,
            "Zotero-API-Version": "3",
            "Accept": "application/pdf",
        },
    )
    with urlopen(request, timeout=45) as response:
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
        payload = response.read(25 * 1024 * 1024 + 1)
    if len(payload) > 25 * 1024 * 1024:
        raise RuntimeError("Zotero PDF attachment exceeds the 25 MB read limit.")
    return payload, content_type


def _current_zotero_user_library_id(api_key: str) -> str:
    key_info = _read_zotero_api_json("/keys/current", api_key=api_key, params={})
    if not isinstance(key_info, dict):
        raise RuntimeError("Zotero key verification returned an invalid response.")
    access = key_info.get("access")
    user_access = access.get("user") if isinstance(access, dict) else None
    if not isinstance(user_access, dict) or user_access.get("library") is not True:
        raise RuntimeError("The configured Zotero API key cannot read its user library.")
    user_id = str(key_info.get("userID") or "").strip()
    if not user_id:
        raise RuntimeError("Zotero key verification did not return a user library ID.")
    return user_id


def read_zotero_api_key_capabilities() -> dict[str, Any]:
    """Return a secret-free capability receipt for the configured Zotero key."""

    api_key = context_env_value("ZOTERO_API_KEY").strip()
    if not api_key:
        raise RuntimeError("Live Zotero API access requires ZOTERO_API_KEY.")
    key_info = _read_zotero_api_json("/keys/current", api_key=api_key, params={})
    if not isinstance(key_info, dict):
        raise RuntimeError("Zotero key verification returned an invalid response.")
    access = key_info.get("access")
    user_access = access.get("user") if isinstance(access, dict) else None
    if not isinstance(user_access, dict):
        user_access = {}
    return {
        "status": "success",
        "provider_read": True,
        "operation": "verify_api_key_capabilities",
        "user_id_present": bool(str(key_info.get("userID") or "").strip()),
        "user_library": user_access.get("library") is True,
        "user_files": user_access.get("files") is True,
        "user_notes": user_access.get("notes") is True,
        "user_write": user_access.get("write") is True,
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_read_api_metadata(
    library_id: str = "",
    library_type: str = "user",
    collection_key: str = "",
    item_key: str = "",
    query: str = "",
    tag: str = "",
    limit: int = 25,
    sort: str = "",
    direction: str = "",
    top_level_only: bool = False,
    item_type: str = "",
    require_abstract: bool = False,
    selection_rank: int = 1,
    selection_count: int = 1,
    include_abstract_text: bool = True,
    live: bool = False,
    continuation: ZoteroMetadataContinuation | None = None,
) -> str:
    """Read bounded Zotero metadata using title/creator `query` or exact `tag`."""

    if continuation is not None:
        continuation = ZoteroMetadataContinuation.model_validate(continuation)
        supplied = {
            name: value for name, value in locals().copy().items()
            if name in ZoteroMetadataScope.model_fields
        }
        defaults = ZoteroMetadataScope(library_id="").model_dump()
        scope = continuation.scope
        for name, value in supplied.items():
            if value != defaults[name] and value != getattr(scope, name):
                raise ValueError(f"Zotero continuation cannot change {name}.")
        library_id, library_type = scope.library_id, scope.library_type
        collection_key, item_key = scope.collection_key, scope.item_key
        query, tag, limit = scope.query, scope.tag, scope.limit
        sort, direction = scope.sort, scope.direction
        top_level_only, item_type = scope.top_level_only, scope.item_type
        require_abstract = scope.require_abstract
        selection_rank, selection_count = scope.selection_rank, scope.selection_count
        include_abstract_text = scope.include_abstract_text
    if library_type not in {"user", "group"}:
        raise ValueError("library_type must be user or group.")
    clean_selection_rank = int(selection_rank or 1)
    if not 1 <= clean_selection_rank <= 10:
        raise ValueError("selection_rank must be between 1 and 10.")
    clean_selection_count = int(selection_count or 1)
    if not 1 <= clean_selection_count <= 10:
        raise ValueError("selection_count must be between 1 and 10.")
    bounded_limit = min(
        max(
            int(limit or 25),
            clean_selection_rank + clean_selection_count - 1,
        ),
        100,
    )
    params = {"limit": bounded_limit}
    if continuation is not None:
        params["start"] = continuation.next_start
        params["limit"] = min(bounded_limit, 1000 - continuation.next_start)
    if query.strip():
        params["q"] = query.strip()
    if tag.strip():
        params["tag"] = tag.strip()
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
                "selection_rule": (
                    "first_nonempty_abstract_in_provider_order"
                    if require_abstract and clean_selection_rank == 1
                    else "ranked_nonempty_abstract_in_provider_order"
                    if require_abstract
                    else "provider_order"
                    if clean_selection_rank == 1
                    else "ranked_item_in_provider_order"
                ),
                "selection_rank": clean_selection_rank,
                "selection_count": clean_selection_count,
                "provider_order": {
                    "sort": clean_sort,
                    "direction": clean_direction,
                    "top_level_only": bool(top_level_only),
                    "item_type": clean_item_type,
                },
                "require_abstract": bool(require_abstract),
                "include_abstract_text": bool(include_abstract_text),
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
    explicit_library_id = library_id.strip()
    resolved_library_id = explicit_library_id or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key:
        raise RuntimeError("Live Zotero API reads require ZOTERO_API_KEY.")
    path = _zotero_api_path(
        library_type=library_type,
        library_id=resolved_library_id,
        collection_key=collection_key,
        item_key=item_key,
        top_level_only=top_level_only,
    )
    library_id_resolution = "explicit" if explicit_library_id else "configured"
    try:
        payload = _read_zotero_api_json(path, api_key=api_key, params=params)
    except HTTPError as exc:
        clean_library_type = str(library_type or "").strip().lower()
        may_resolve_current_user = (
            exc.code == 403 and not explicit_library_id and clean_library_type != "group"
        )
        if not may_resolve_current_user:
            raise
        resolved_library_id = _current_zotero_user_library_id(api_key)
        path = _zotero_api_path(
            library_type="user",
            library_id=resolved_library_id,
            collection_key=collection_key,
            item_key=item_key,
            top_level_only=top_level_only,
        )
        payload = _read_zotero_api_json(path, api_key=api_key, params=params)
        library_id_resolution = "api_key_current_user_after_configured_403"
    if not isinstance(payload, list | dict):
        raise RuntimeError("Zotero metadata returned an invalid response.")
    coverage = _zotero_page_coverage(
        payload,
        path=path,
        start=continuation.next_start if continuation else 0,
        limit=int(params["limit"]),
        pages_read=continuation.pages_read + 1 if continuation else 1,
        expected_version=continuation.library_version if continuation else None,
    )
    items = payload if isinstance(payload, list) else [payload]
    if require_abstract:
        items = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("data"), dict)
            and str(item["data"].get("abstractNote") or "").strip()
        ]
    available_item_count = len(items)
    eligible_seen = continuation.eligible_items_seen if continuation else 0
    selection_start = max(0, clean_selection_rank - 1 - eligible_seen)
    selection_end = max(0, clean_selection_rank - 1 + clean_selection_count - eligible_seen)
    items = items[selection_start:selection_end]
    next_page = None
    if coverage["continuation_supported"]:
        next_page = ZoteroMetadataContinuation(
            scope=ZoteroMetadataScope(
                library_id=resolved_library_id, library_type=library_type,
                collection_key=collection_key, item_key=item_key, query=query, tag=tag,
                limit=bounded_limit, sort=clean_sort, direction=clean_direction,
                top_level_only=top_level_only, item_type=clean_item_type,
                require_abstract=require_abstract, selection_rank=clean_selection_rank,
                selection_count=clean_selection_count, include_abstract_text=include_abstract_text,
            ),
            next_start=coverage["next_start"], pages_read=coverage["pages_read"],
            eligible_items_seen=eligible_seen + available_item_count,
            library_version=coverage["library_version"],
        ).model_dump(mode="json")
    selected_data = (
        items[0].get("data")
        if items and isinstance(items[0], dict) and isinstance(items[0].get("data"), dict)
        else {}
    )
    selected_item_has_abstract = bool(str(selected_data.get("abstractNote") or "").strip())
    if not include_abstract_text:
        items = [_zotero_item_without_abstract_text(item) for item in items]
    return _json_payload(
        {
            "status": "success" if items else "partial" if coverage["has_more"] else "not_found",
            "api_base_url": ZOTERO_API_BASE_URL,
            "path": path,
            "params": params,
            "provider_read": True,
            "library_id_resolution": library_id_resolution,
            "selection_rule": (
                "first_nonempty_abstract_in_provider_order"
                if require_abstract and clean_selection_rank == 1
                else "ranked_nonempty_abstract_in_provider_order"
                if require_abstract
                else "provider_order"
                if clean_selection_rank == 1
                else "ranked_item_in_provider_order"
            ),
            "selection_rank": clean_selection_rank,
            "selection_count": clean_selection_count,
            "available_item_count": available_item_count,
            "available_item_count_scope": "current_page_after_filtering",
            "absence_scope": "complete_query" if coverage["complete"] else "current_page",
            "coverage": coverage,
            "continuation": next_page,
            "provider_order": {
                "sort": clean_sort,
                "direction": clean_direction,
                "top_level_only": bool(top_level_only),
                "item_type": clean_item_type,
            },
            "require_abstract": bool(require_abstract),
            "include_abstract_text": bool(include_abstract_text),
            "items": items,
            "item_count": len(items),
            "identity_fingerprints": identity_fingerprints(
                (
                    item.get("key")
                    or (
                        item.get("data", {}).get("key")
                        if isinstance(item.get("data"), Mapping)
                        else ""
                    )
                )
                for item in items
                if isinstance(item, Mapping)
            ),
            "selected_item_title": str(selected_data.get("title") or ""),
            "selected_item_key": str(
                items[0].get("key") if items and isinstance(items[0], dict) else ""
            ),
            "selected_item_has_abstract": selected_item_has_abstract,
            "selected_item_date_added": str(selected_data.get("dateAdded") or ""),
            "send_enabled": False,
            "zotero_write_supported": False,
        }
    )


def _zotero_item_without_abstract_text(item: Any) -> Any:
    """Preserve abstract presence while withholding the abstract body."""

    if not isinstance(item, dict):
        return item
    projected = dict(item)
    data = item.get("data")
    if not isinstance(data, dict):
        return projected
    projected_data = dict(data)
    projected_data["abstractPresent"] = bool(
        str(projected_data.get("abstractNote") or "").strip()
    )
    projected_data.pop("abstractNote", None)
    projected["data"] = projected_data
    return projected


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_read_item_children(
    parent_item_key: str,
    library_id: str = "",
    library_type: str = "user",
    limit: int = 50,
    live: bool = False,
    continuation: ZoteroChildrenContinuation | None = None,
    note_continuation: ZoteroNoteContinuation | None = None,
) -> str:
    """Read bounded notes and attachment metadata for one exact Zotero parent item."""

    clean_parent = str(parent_item_key or "").strip()
    if not clean_parent:
        raise ValueError("parent_item_key is required for Zotero child reads.")
    if continuation is not None and note_continuation is not None:
        raise ValueError("Continue either a child page or one note, not both.")
    if continuation is not None:
        continuation = ZoteroChildrenContinuation.model_validate(continuation)
    if note_continuation is not None:
        note_continuation = ZoteroNoteContinuation.model_validate(note_continuation)
    cursor = continuation or note_continuation
    if cursor is not None:
        if clean_parent != cursor.parent_item_key:
            raise ValueError("Zotero continuation cannot change the parent item.")
        if library_id and library_id != cursor.library_id:
            raise ValueError("Zotero continuation cannot change the library.")
        if library_type != "user" and library_type != cursor.library_type:
            raise ValueError("Zotero continuation cannot change the library type.")
        library_id, library_type = cursor.library_id, cursor.library_type
    if continuation is not None:
        if limit != 50 and limit != continuation.limit:
            raise ValueError("Zotero continuation cannot change the page limit.")
        limit = continuation.limit
    if library_type not in {"user", "group"}:
        raise ValueError("library_type must be user or group.")
    bounded_limit = min(max(int(limit or 50), 1), 100)
    explicit_library_id = str(library_id or "").strip()
    planned_path = _zotero_api_path(
        library_type=library_type,
        library_id=explicit_library_id or "configured_library_id",
        collection_key="",
        item_key=clean_parent,
    ) + "/children"
    if not live:
        return _json_payload(
            {
                "status": "dry-run",
                "planned_path": planned_path,
                "parent_item_key": clean_parent,
                "limit": bounded_limit,
                "supported_child_types": ["note", "attachment"],
                "note_continuation": note_continuation.model_dump() if note_continuation else None,
                "send_enabled": False,
                "zotero_write_supported": False,
            }
        )
    api_key = context_env_value("ZOTERO_API_KEY").strip()
    resolved_library_id = explicit_library_id or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key:
        raise RuntimeError("Live Zotero API reads require ZOTERO_API_KEY.")

    def child_path(current_library_id: str, current_library_type: str) -> str:
        return _zotero_api_path(
            library_type=current_library_type,
            library_id=current_library_id,
            collection_key="",
            item_key=note_continuation.note_item_key if note_continuation else clean_parent,
        ) + ("" if note_continuation else "/children")

    path = child_path(resolved_library_id, library_type)
    library_id_resolution = "explicit" if explicit_library_id else "configured"
    params = {} if note_continuation else {"limit": bounded_limit}
    if continuation is not None:
        params["start"] = continuation.next_start
        params["limit"] = min(bounded_limit, 1000 - continuation.next_start)
    try:
        payload = _read_zotero_api_json(path, api_key=api_key, params=params)
    except HTTPError as exc:
        may_resolve_current_user = (
            exc.code == 403
            and not explicit_library_id
            and str(library_type or "").strip().lower() != "group"
        )
        if not may_resolve_current_user:
            raise
        resolved_library_id = _current_zotero_user_library_id(api_key)
        path = child_path(resolved_library_id, "user")
        payload = _read_zotero_api_json(path, api_key=api_key, params=params)
        library_id_resolution = "api_key_current_user_after_configured_403"
    if note_continuation:
        if not isinstance(payload, dict) or _zotero_item_data(payload).get("itemType") != "note":
            raise RuntimeError("Zotero note identity or type changed; restart the read.")
    elif not isinstance(payload, list):
        raise RuntimeError("Zotero children returned an invalid response.")
    coverage = _zotero_page_coverage(
        payload, path=path, start=continuation.next_start if continuation else 0,
        limit=int(params.get("limit", 1)),
        pages_read=continuation.pages_read + 1 if continuation else 1,
        expected_version=continuation.library_version if continuation else None,
    )
    children = [payload] if note_continuation else payload if isinstance(payload, list) else []
    next_page = None
    if not note_continuation and coverage["continuation_supported"]:
        next_page = ZoteroChildrenContinuation(
            library_id=resolved_library_id, library_type=library_type,
            parent_item_key=clean_parent, limit=bounded_limit,
            next_start=coverage["next_start"], pages_read=coverage["pages_read"],
            library_version=coverage["library_version"],
        ).model_dump(mode="json")
    projected_children: list[dict[str, Any]] = []
    for child in children[:int(params.get("limit", bounded_limit))]:
        if not isinstance(child, dict):
            continue
        data = child.get("data") if isinstance(child.get("data"), dict) else {}
        item_type = str(data.get("itemType") or "").strip()
        if item_type not in {"note", "attachment"}:
            continue
        if str(data.get("parentItem") or "").strip() != clean_parent:
            raise RuntimeError("The selected Zotero child does not belong to the parent item.")
        child_key = str(child.get("key") or data.get("key") or "").strip()
        version = child.get("version", data.get("version"))
        raw_note = str(data.get("note") or "")
        source_sha256 = hashlib.sha256(raw_note.encode("utf-8")).hexdigest()
        note_text = html.unescape(re.sub(r"<[^>]+>", " ", raw_note))
        note_text = " ".join(note_text.split())
        offset = note_continuation.next_char if note_continuation else 0
        if note_continuation is not None and (
            item_type != "note" or child_key != note_continuation.note_item_key
            or version != note_continuation.version
            or source_sha256 != note_continuation.source_sha256
        ):
            raise RuntimeError("Zotero note identity, version or source changed; restart the read.")
        if offset > len(note_text):
            raise ValueError("Zotero note continuation starts beyond the source text.")
        end = min(offset + 12000, len(note_text))
        windows_read = note_continuation.windows_read + 1 if note_continuation else 1
        next_note = None
        note_limitations = (
            ["Normalized note text does not interpret HTML layout, links, tables or images."]
            if item_type == "note" else []
        )
        if end < len(note_text):
            if isinstance(version, int) and version >= 0 and child_key and windows_read < 10:
                next_note = ZoteroNoteContinuation(
                    library_id=resolved_library_id, library_type=library_type,
                    parent_item_key=clean_parent, note_item_key=child_key, version=version,
                    source_sha256=source_sha256, next_char=end, windows_read=windows_read,
                ).model_dump(mode="json")
            else:
                note_limitations.append("Note continuation needs source identity/version and is capped at 10 windows.")
        projected_children.append(
            {
                "item_key": child_key,
                "version": version,
                "item_type": item_type,
                "parent_item_key": str(data.get("parentItem") or "").strip(),
                "title": str(data.get("title") or "").strip(),
                "filename": str(data.get("filename") or "").strip(),
                "content_type": str(data.get("contentType") or "").strip(),
                "link_mode": str(data.get("linkMode") or "").strip(),
                "url": str(data.get("url") or "").strip(),
                "note": raw_note[:12000] if not note_continuation else "",
                "note_html_included": not bool(note_continuation),
                "note_html_truncated": len(raw_note) > 12000,
                "note_text": note_text[offset:end],
                "note_text_coverage": {
                    "representation": "normalized_plain_text",
                    "char_start": offset, "char_end": end, "total_chars": len(note_text),
                    "complete": offset == 0 and end == len(note_text),
                    "has_more": end < len(note_text), "source_sha256": source_sha256,
                    "windows_read": windows_read, "limitations": note_limitations,
                },
                "note_continuation": next_note,
                "tags": data.get("tags") if isinstance(data.get("tags"), list) else [],
                "date_added": str(data.get("dateAdded") or "").strip(),
                "date_modified": str(data.get("dateModified") or "").strip(),
            }
        )
    return _json_payload(
        {
            "status": "success",
            "provider_read": True,
            "path": path,
            "library_id_resolution": library_id_resolution,
            "parent_item_key": clean_parent,
            "library_id": resolved_library_id,
            "library_type": library_type,
            "coverage": coverage,
            "continuation": next_page,
            "children": projected_children,
            "child_count": len(projected_children),
            "note_count": sum(item["item_type"] == "note" for item in projected_children),
            "attachment_count": sum(
                item["item_type"] == "attachment" for item in projected_children
            ),
            "send_enabled": False,
            "zotero_write_supported": False,
        }
    )


def _zotero_pdf_visual_flags(page: Any) -> tuple[bool, bool, bool]:
    """Inspect page/form operators; do not render, OCR or decode image pixels."""

    raster = vector = False
    visited: set[int] = set()

    def inspect_resources(node: Any, depth: int = 0) -> None:
        nonlocal raster, vector
        if depth > 12 or len(visited) > 100:
            raise ValueError("PDF visual resource inspection limit reached.")
        node = node.get_object() if hasattr(node, "get_object") else node
        if id(node) in visited:
            return
        visited.add(id(node))
        if node.get("/Subtype") == "/Image":
            raster = True
        resources = node.get("/Resources", {})
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        objects = resources.get("/XObject", {})
        objects = objects.get_object() if hasattr(objects, "get_object") else objects
        for child in objects.values():
            inspect_resources(child, depth + 1)
        if hasattr(node, "get_contents"):
            contents = node.get_contents()
        elif hasattr(node, "get_data") and node.get("/Subtype") == "/Form":
            from pypdf.generic import ContentStream

            contents = ContentStream(node, getattr(page, "pdf", None))
        else:
            contents = None
        for _, operator in getattr(contents, "operations", []):
            raster = raster or operator == b"INLINE IMAGE"
            vector = vector or operator in {b"m", b"l", b"re", b"S", b"f", b"f*", b"sh"}

    try:
        inspect_resources(page)
    except Exception:
        return raster, vector, False
    return raster, vector, True


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_read_pdf_attachment_text(
    parent_item_key: str,
    attachment_item_key: str,
    library_id: str = "",
    library_type: str = "user",
    max_pages: int = 25,
    max_chars: int = 50000,
    live: bool = False,
    continuation: ZoteroPdfContinuation | None = None,
) -> str:
    """Read bounded text from one exact PDF attachment without persisting the file."""

    clean_parent = str(parent_item_key or "").strip()
    clean_attachment = str(attachment_item_key or "").strip()
    if not clean_parent or not clean_attachment:
        raise ValueError("parent_item_key and attachment_item_key are required.")
    if continuation is not None:
        continuation = ZoteroPdfContinuation.model_validate(continuation)
        if (clean_parent, clean_attachment) != (
            continuation.parent_item_key, continuation.attachment_item_key,
        ):
            raise ValueError("Zotero PDF continuation cannot change the parent or attachment.")
        if library_id and library_id != continuation.library_id:
            raise ValueError("Zotero PDF continuation cannot change the library.")
        if library_type != "user" and library_type != continuation.library_type:
            raise ValueError("Zotero PDF continuation cannot change the library type.")
        if (max_pages != 25 and max_pages != continuation.max_pages) or (
            max_chars != 50000 and max_chars != continuation.max_chars
        ):
            raise ValueError("Zotero PDF continuation cannot change the read limits.")
        library_id, library_type = continuation.library_id, continuation.library_type
        max_pages, max_chars = continuation.max_pages, continuation.max_chars
    if library_type not in {"user", "group"}:
        raise ValueError("library_type must be user or group.")
    bounded_pages = min(max(int(max_pages or 25), 1), 100)
    bounded_chars = min(max(int(max_chars or 50000), 1000), 200000)
    explicit_library_id = str(library_id or "").strip()
    if not live:
        return _json_payload(
            {
                "status": "dry-run",
                "parent_item_key": clean_parent,
                "attachment_item_key": clean_attachment,
                "max_pages": bounded_pages,
                "max_chars": bounded_chars,
                "file_persisted": False,
                "send_enabled": False,
                "zotero_write_supported": False,
            }
        )
    api_key = context_env_value("ZOTERO_API_KEY").strip()
    resolved_library_id = explicit_library_id or context_env_value("ZOTERO_LIBRARY_ID").strip()
    if not api_key:
        raise RuntimeError("Live Zotero API reads require ZOTERO_API_KEY.")

    def item_path(current_library_id: str, current_library_type: str) -> str:
        return _zotero_api_path(
            library_type=current_library_type,
            library_id=current_library_id,
            collection_key="",
            item_key=clean_attachment,
        )

    path = item_path(resolved_library_id, library_type)
    library_id_resolution = "explicit" if explicit_library_id else "configured"
    try:
        attachment = _read_zotero_api_json(path, api_key=api_key, params={})
    except HTTPError as exc:
        may_resolve_current_user = (
            exc.code == 403
            and not explicit_library_id
            and str(library_type or "").strip().lower() != "group"
        )
        if not may_resolve_current_user:
            raise
        resolved_library_id = _current_zotero_user_library_id(api_key)
        path = item_path(resolved_library_id, "user")
        attachment = _read_zotero_api_json(path, api_key=api_key, params={})
        library_id_resolution = "api_key_current_user_after_configured_403"
    data = attachment.get("data") if isinstance(attachment, dict) else {}
    if not isinstance(data, dict):
        raise RuntimeError("Zotero attachment metadata returned an invalid object.")
    if str(data.get("itemType") or "") != "attachment":
        raise RuntimeError("The selected Zotero child is not an attachment.")
    if str(data.get("parentItem") or "").strip() != clean_parent:
        raise RuntimeError("The selected Zotero attachment does not belong to the parent item.")
    source_key = str(attachment.get("key") or data.get("key") or "")
    source_version = attachment.get("version", data.get("version"))
    if source_key and source_key != clean_attachment:
        raise RuntimeError("Zotero attachment identity does not match the selected item.")
    if continuation and (source_key != clean_attachment or source_version != continuation.version):
        raise RuntimeError("Zotero attachment identity or version changed; restart the read.")
    content_type = str(data.get("contentType") or "").lower().strip()
    filename = str(data.get("filename") or data.get("title") or "").strip()
    if content_type != "application/pdf" and not filename.lower().endswith(".pdf"):
        raise RuntimeError("The selected Zotero attachment is not a PDF.")
    file_payload, response_content_type = _read_zotero_api_bytes(
        path + "/file",
        api_key=api_key,
    )
    if not file_payload.startswith(b"%PDF-"):
        raise RuntimeError("Zotero attachment bytes did not have a valid PDF signature.")
    source_sha256 = hashlib.sha256(file_payload).hexdigest()
    if continuation and source_sha256 != continuation.source_sha256:
        raise RuntimeError("Zotero attachment bytes changed; restart the read.")
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - dependency boundary
        raise RuntimeError("PDF text extraction requires optional pypdf.") from exc
    reader = PdfReader(io.BytesIO(file_payload))
    page_count = len(reader.pages)
    start_page = continuation.next_page if continuation else 0
    start_char = continuation.next_char if continuation else 0
    if start_page >= page_count and continuation:
        raise ValueError("Zotero PDF continuation starts beyond the source pages.")
    text_parts: list[str] = []
    pages: list[dict[str, Any]] = []
    next_page, next_char = start_page, start_char
    remaining = bounded_chars
    for index in range(start_page, min(page_count, start_page + bounded_pages)):
        if remaining <= 2:
            break
        page = reader.pages[index]
        text = str(page.extract_text() or "")
        offset = start_char if index == start_page else 0
        if offset > len(text):
            raise ValueError("Zotero PDF continuation starts beyond the page text.")
        raster, vector, visual_inspected = _zotero_pdf_visual_flags(page)
        allowance = remaining - (2 if text_parts else 0)
        piece = text[offset:offset + allowance]
        text_parts.append(piece)
        remaining -= len(piece) + (2 if len(text_parts) > 1 else 0)
        end = offset + len(piece)
        pages.append({
            "page_number": index + 1, "char_start": offset, "char_end": end,
            "total_text_chars": len(text), "has_text": bool(text.strip()),
            "text_complete": offset == 0 and end == len(text),
            "raster_content_detected": raster, "vector_content_detected": vector,
            "visual_inspection_complete": visual_inspected,
            "visual_content_read": False,
            "ocr_required": raster or not bool(text.strip()),
        })
        if end < len(text):
            next_page, next_char = index, end
            break
        next_page, next_char = index + 1, 0
    extracted = "\n\n".join(text_parts)
    truncated = next_page < page_count
    visual_gap = any(
        item["raster_content_detected"] or item["vector_content_detected"] or not item["has_text"]
        for item in pages
    )
    limitations = ["Selectable-text extraction does not verify visual meaning, layout or table relationships."]
    if visual_gap:
        limitations.append("Image, drawing or text-empty pages remain unread; this Zotero tool does not support OCR or visual interpretation.")
    if any(not item["visual_inspection_complete"] for item in pages):
        limitations.append("Visual resource inspection was incomplete for one or more pages.")
    windows_read = continuation.windows_read + 1 if continuation else 1
    next_window = None
    if truncated:
        if source_key == clean_attachment and isinstance(source_version, int) and source_version >= 0 and windows_read < 10:
            next_window = ZoteroPdfContinuation(
                library_id=resolved_library_id, library_type=library_type,
                parent_item_key=clean_parent, attachment_item_key=clean_attachment,
                version=source_version, source_sha256=source_sha256,
                next_page=next_page, next_char=next_char,
                max_pages=bounded_pages, max_chars=bounded_chars, windows_read=windows_read,
            ).model_dump(mode="json")
        else:
            limitations.append("PDF continuation needs source identity/version and is capped at 10 windows.")
    return _json_payload(
        {
            "status": "partial" if visual_gap else "success" if extracted.strip() else "unsupported",
            "provider_read": True,
            "library_id_resolution": library_id_resolution,
            "parent_item_key": clean_parent,
            "attachment_item_key": clean_attachment,
            "library_id": resolved_library_id,
            "library_type": library_type,
            "version": source_version,
            "filename": filename,
            "content_type": content_type or response_content_type,
            "page_count": page_count,
            "pages_read": len(pages),
            "pages": pages,
            "text": extracted,
            "char_count": len(extracted),
            "truncated": truncated,
            "coverage": "partial" if truncated or visual_gap else "selectable_text_only",
            "ocr_supported": False,
            "visual_content_read": False,
            "limitations": limitations,
            "continuation": next_window,
            "sha256": source_sha256,
            "file_persisted": False,
            "send_enabled": False,
            "zotero_write_supported": False,
        }
    )


def read_latest_zotero_journal_metadata(
    *,
    require_abstract: bool = False,
    selection_rank: int = 1,
) -> dict[str, Any]:
    """Read a ranked journal article in latest-first provider order."""

    selection_options = (
        {"selection_rank": selection_rank}
        if selection_rank != 1
        else {}
    )
    payload = json.loads(
        zotero_read_api_metadata(
            limit=100 if require_abstract else selection_rank,
            sort="dateAdded",
            direction="desc",
            top_level_only=True,
            item_type="journalArticle",
            require_abstract=require_abstract,
            live=True,
            **selection_options,
        )
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Zotero metadata read returned a non-object payload.")
    return payload


def read_latest_zotero_journal_abstract_metadata(
    *,
    selection_rank: int = 1,
) -> dict[str, Any]:
    """Read a ranked journal article with an abstract in latest-first order."""

    return read_latest_zotero_journal_metadata(
        require_abstract=True,
        selection_rank=selection_rank,
    )


_ZOTERO_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("item_type", "itemType"),
    ("title", "title"),
    ("abstract", "abstractNote"),
    ("publication_title", "publicationTitle"),
    ("journal_abbreviation", "journalAbbreviation"),
    ("volume", "volume"),
    ("issue", "issue"),
    ("pages", "pages"),
    ("publication_date", "date"),
    ("series", "series"),
    ("series_title", "seriesTitle"),
    ("language", "language"),
    ("doi", "DOI"),
    ("issn", "ISSN"),
    ("isbn", "ISBN"),
    ("pmid", "PMID"),
    ("short_title", "shortTitle"),
    ("url", "url"),
    ("access_date", "accessDate"),
    ("publisher", "publisher"),
    ("place", "place"),
    ("rights", "rights"),
    ("archive", "archive"),
    ("archive_location", "archiveLocation"),
    ("library_catalog", "libraryCatalog"),
    ("call_number", "callNumber"),
    ("extra", "extra"),
    ("date_added", "dateAdded"),
    ("date_modified", "dateModified"),
    ("tags", "tags"),
    ("collections", "collections"),
    ("relations", "relations"),
)

_ZOTERO_FIELD_ALIASES: tuple[tuple[str, str], ...] = (
    ("title", r"\btitles?\b"),
    ("authors", r"\bauthors?|creators?\b"),
    ("abstract", r"\babstract\b"),
    ("publication_title", r"\bpublication\s+title\b|\bjournal(?:\s+name)?\b|\bpublished\s+in\b"),
    ("publication_date", r"\bpublication\s+date\b|\bpublished\s+(?:on|when)\b"),
    ("doi", r"\bdoi\b"),
    ("issn", r"\bissn\b"),
    ("isbn", r"\bisbn\b"),
    ("pmid", r"\bpmid\b|\bpubmed\s+id\b"),
    ("url", r"\burls?|links?\b"),
    ("volume", r"\bvolume\b"),
    ("issue", r"\bissue\b"),
    ("pages", r"\bpages?\b"),
    ("publisher", r"\bpublisher\b"),
    ("language", r"\blanguage\b"),
    ("tags", r"\btags?\b"),
    ("collections", r"\bcollections?\b"),
    ("rights", r"\brights?|license\b"),
    ("extra", r"\bextra\s+(?:field|metadata)\b"),
    ("date_added", r"\bdate\s+added\b|\badded\s+(?:on|when)\b"),
    ("date_modified", r"\bdate\s+modified\b|\bmodified\s+(?:on|when)\b"),
)


def _bounded_zotero_provider_value(value: Any, *, depth: int = 0) -> Any:
    """Keep provider metadata inspectable without admitting unbounded nested payloads."""

    if depth >= 4:
        return "[nested value omitted]"
    if isinstance(value, str):
        return value[:12000]
    if isinstance(value, list):
        return [
            _bounded_zotero_provider_value(item, depth=depth + 1)
            for item in value[:100]
        ]
    if isinstance(value, dict):
        return {
            str(key): _bounded_zotero_provider_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    return value


def project_zotero_item_metadata(
    item: dict[str, Any],
    *,
    request_text: str = "",
    requested_fields: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    """Project requested Zotero fields without losing provider field identity."""

    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    available: dict[str, Any] = {}
    for canonical_name, provider_name in _ZOTERO_METADATA_FIELDS:
        value = data.get(provider_name)
        if value not in (None, "", [], {}):
            available[canonical_name] = value
    creators = data.get("creators")
    creator_names = zotero_creator_names(creators)
    if creator_names:
        available["authors"] = creator_names
    if isinstance(creators, list) and creators:
        available["creators"] = creators

    normalized_request = " ".join(str(request_text or "").lower().split())
    if requested_fields is None:
        selected_fields = [
            field
            for field, pattern in _ZOTERO_FIELD_ALIASES
            if re.search(pattern, normalized_request)
        ]
        broad_metadata_request = bool(
            re.search(
                r"\b(?:all|available|complete|full)\s+(?:item\s+)?"
                r"(?:metadata|details|fields|information)\b",
                normalized_request,
            )
        )
    else:
        normalized_fields = list(
            dict.fromkeys(
                str(field or "").strip().lower() for field in requested_fields
            )
        )
        allowed_fields = {field for field, _pattern in _ZOTERO_FIELD_ALIASES}
        selected_fields = [
            field for field in normalized_fields if field in allowed_fields
        ]
        broad_metadata_request = "metadata" in normalized_fields
    if broad_metadata_request:
        selected_fields = list(available)
    if not selected_fields:
        selected_fields = ["title"]
    selected_fields = list(dict.fromkeys(selected_fields))
    projected = {field: available.get(field, "") for field in selected_fields}
    provider_field_map = dict(_ZOTERO_METADATA_FIELDS)
    provider_field_map.update({"authors": "creators", "creators": "creators"})
    return {
        "schema": "keystone.zotero_item_metadata_projection.v1",
        "item_key": str(item.get("key") or data.get("key") or "").strip(),
        "version": item.get("version"),
        "requested_fields": requested_fields,
        "fields": projected,
        "missing_requested_fields": [
            field for field, value in projected.items() if value in (None, "", [], {})
        ],
        "available_field_names": sorted(available),
        "available_provider_field_names": sorted(str(key) for key in data),
        "provider_field_map": {
            canonical_name: provider_field_map[canonical_name]
            for canonical_name in selected_fields
            if canonical_name in provider_field_map
        },
        "provider_fields": (
            _bounded_zotero_provider_value(data) if broad_metadata_request else {}
        ),
        "source": "zotero_api_metadata",
    }


def enrich_zotero_bibliographic_metadata(
    *,
    doi: str,
    title: str,
    item_key: str,
) -> dict[str, Any]:
    """Resolve missing citation fields from DOI metadata without page/full-text search."""

    clean_doi = str(doi or "").strip()
    if not clean_doi:
        return {"status": "missing_identifier", "authors": [], "publication_title": ""}
    result = enrich_source_reference(
        url=f"https://doi.org/{clean_doi}",
        title=title,
        source_id=f"zotero:item:{item_key}",
        live=True,
    )
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return {
        "status": result.status,
        "authors": list(metadata.get("authors") or []),
        "publication_title": str(metadata.get("publication_title") or "").strip(),
        "doi": str(metadata.get("doi") or clean_doi).strip(),
        "source": "crossref_doi_metadata",
    }


def zotero_creator_names(creators: Any) -> list[str]:
    """Normalize Zotero person and organization creators into display names."""

    if not isinstance(creators, list):
        return []
    names: list[str] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        literal = str(creator.get("name") or "").strip()
        first_name = str(creator.get("firstName") or "").strip()
        last_name = str(creator.get("lastName") or "").strip()
        name = literal or " ".join(part for part in (first_name, last_name) if part)
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


@durable_provider_tool("zotero_write_test_note")
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
    clean_approval = str(
        approval_reference or os.getenv(ZOTERO_OPERATOR_APPROVAL_ENV, "")
    ).strip()
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
            "approval_reference": clean_approval,
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "zotero_test_note_write_supported": True,
        }

    api_key, resolved_library_id = _zotero_live_write_config(
        library_id=library_id,
        approval_reference=clean_approval,
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

    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": clean_operation,
        "item_key": resolved_item_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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
        "provider_link": _zotero_provider_link(after),
        "parent_item_key": str(after_data.get("parentItem") or ""),
        "required_marker": ZOTERO_TEST_NOTE_MARKER,
        "approval_reference": clean_approval,
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


@durable_provider_tool("zotero_delete_test_note")
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
    clean_approval = str(
        approval_reference or os.getenv(ZOTERO_OPERATOR_APPROVAL_ENV, "")
    ).strip()
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
            "approval_reference": clean_approval,
            "verification": {"status": "preview", "passed": False},
            "send_enabled": False,
            "zotero_test_note_write_supported": True,
        }

    api_key, resolved_library_id = _zotero_live_write_config(
        library_id=library_id,
        approval_reference=clean_approval,
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
    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": "delete_test_note",
        "item_key": clean_item_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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
        "provider_link": _zotero_provider_link(before),
        "required_marker": ZOTERO_TEST_NOTE_MARKER,
        "approval_reference": clean_approval,
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


def zotero_test_note_lifecycle_impl(
    *,
    approval_reference: str = "",
    library_id: str = "",
    library_type: str = "user",
    live: bool = False,
) -> dict[str, Any]:
    """Create, verify, revise, verify, and remove one marked standalone note."""

    clean_approval = str(
        approval_reference or os.getenv(ZOTERO_OPERATOR_APPROVAL_ENV, "")
    ).strip()
    if live and not clean_approval:
        raise RuntimeError(
            "The Zotero test-note lifecycle requires a non-empty approval_reference."
        )
    suffix = uuid4().hex[:10]
    created_html = f"<p>{ZOTERO_TEST_NOTE_MARKER} {suffix} created for validation</p>"
    updated_html = f"<p>{ZOTERO_TEST_NOTE_MARKER} {suffix} revised and verified</p>"
    create_result: dict[str, Any] = {}
    update_result: dict[str, Any] = {}
    delete_result: dict[str, Any] = {}
    item_key = ""
    failure = ""
    try:
        create_result = zotero_write_test_note_impl(
            created_html,
            library_id=library_id,
            library_type=library_type,
            approval_reference=f"{clean_approval}:create" if clean_approval else "",
            operation="create",
            live=live,
        )
        item_key = str(create_result.get("item_key") or "").strip()
        if not live:
            return {
                "status": "dry-run",
                "operation": "test_note_lifecycle",
                "required_marker": ZOTERO_TEST_NOTE_MARKER,
                "approval_reference": clean_approval,
                "create": _zotero_lifecycle_step_receipt(create_result),
                "send_enabled": False,
            }
        if not item_key or not _zotero_verification_passed(create_result):
            failure = "Zotero test-note create did not pass provider read-back verification."
        else:
            update_result = zotero_write_test_note_impl(
                updated_html,
                item_key=item_key,
                library_id=library_id,
                library_type=library_type,
                approval_reference=f"{clean_approval}:update",
                operation="update",
                live=True,
            )
            if not _zotero_verification_passed(update_result):
                failure = "Zotero test-note update did not pass provider read-back verification."
    except Exception as exc:  # preserve a bounded failure while still cleaning up
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and item_key:
            try:
                delete_result = zotero_delete_test_note_impl(
                    item_key,
                    library_id=library_id,
                    library_type=library_type,
                    approval_reference=f"{clean_approval}:delete",
                    live=True,
                )
            except Exception as exc:
                delete_result = {
                    "status": "failed",
                    "operation": "delete_test_note",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "verification": {"passed": False},
                    "send_enabled": False,
                }
    create_passed = _zotero_verification_passed(create_result)
    update_passed = _zotero_verification_passed(update_result)
    cleanup_passed = _zotero_verification_passed(delete_result)
    passed = create_passed and update_passed and cleanup_passed and not failure
    return {
        "status": "success" if passed else "failed",
        "operation": "test_note_lifecycle",
        "item_key": item_key,
        "provider_link": str(create_result.get("provider_link") or ""),
        "required_marker": ZOTERO_TEST_NOTE_MARKER,
        "approval_reference": clean_approval,
        "create": _zotero_lifecycle_step_receipt(create_result),
        "update": _zotero_lifecycle_step_receipt(update_result),
        "delete": _zotero_lifecycle_step_receipt(delete_result),
        "verification": {
            "passed": passed,
            "create_read_back": create_passed,
            "same_note_update_read_back": update_passed,
            "note_absent_after_cleanup": cleanup_passed,
        },
        "failure": failure,
        "send_enabled": False,
    }


def _zotero_verification_passed(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _zotero_lifecycle_step_receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "item_key",
            "provider_link",
            "required_marker",
            "approval_reference",
            "verification",
            "send_enabled",
            "reason",
        )
        if key in result
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_test_note_lifecycle(
    approval_reference: str = "",
    library_id: str = "",
    library_type: str = "user",
    live: bool = False,
) -> str:
    """Run one approved KBA_TEST_NOTE lifecycle with versioned read-backs and cleanup."""

    return _json_payload(
        zotero_test_note_lifecycle_impl(
            approval_reference=approval_reference,
            library_id=library_id,
            library_type=library_type,
            live=live,
        )
    )


@durable_provider_tool("zotero_write_test_collection")
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
    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": clean_operation,
        "collection_key": resolved_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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


@durable_provider_tool("zotero_write_test_item")
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
    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": clean_operation,
        "item_key": resolved_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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


@durable_provider_tool("zotero_delete_test_item")
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
    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": "delete_test_item",
        "item_key": clean_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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


@durable_provider_tool("zotero_delete_test_collection")
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
    record_provider_observation({
        "status": "observed", "provider": "zotero", "operation": "delete_test_collection",
        "collection_key": clean_key, "provider_write": True,
        "verification": {"passed": False, "status": "pending_readback"},
    })
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
        "provider_link": _zotero_provider_link(item),
        "version": int(data.get("version") or item.get("version") or 0),
        "item_type": str(data.get("itemType") or ""),
        "parent_item_key": str(data.get("parentItem") or ""),
        "marker_present": _contains_zotero_test_note_marker(data),
        "note_sha256": hashlib.sha256(normalized_note.encode("utf-8")).hexdigest(),
    }


def _zotero_provider_link(item: Mapping[str, Any]) -> str:
    """Prefer Zotero's returned web link, then its exact API self link."""

    links = item.get("links")
    if not isinstance(links, Mapping):
        return ""
    for name in ("alternate", "self"):
        candidate = links.get(name)
        if isinstance(candidate, Mapping):
            href = str(candidate.get("href") or "").strip()
            if href:
                return href
    return ""


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
