"""Zotero context and guarded importer tools for specialist agents."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

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
ZOTERO_CONTEXT_TOOL_NAMES: tuple[str, ...] = (
    *ZOTERO_READ_CONTEXT_TOOL_NAMES,
    *ZOTERO_IMPORT_TOOL_NAMES,
)
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
) -> str:
    clean_type = "groups" if str(library_type or "").strip().lower() == "group" else "users"
    if not library_id.strip():
        raise ValueError("library_id is required for live Zotero API reads.")
    base = f"/{clean_type}/{quote(library_id.strip(), safe='')}"
    if item_key.strip():
        return f"{base}/items/{quote(item_key.strip(), safe='')}"
    if collection_key.strip():
        return f"{base}/collections/{quote(collection_key.strip(), safe='')}/items"
    return f"{base}/items"


@function_tool(**keystone_tool_guardrail_kwargs())
def zotero_read_api_metadata(
    library_id: str = "",
    library_type: str = "user",
    collection_key: str = "",
    item_key: str = "",
    query: str = "",
    limit: int = 25,
    live: bool = False,
) -> str:
    """Read Zotero API metadata for libraries, collections, or items without mutation."""

    bounded_limit = min(max(int(limit or 25), 1), 100)
    params = {"limit": bounded_limit}
    if query.strip():
        params["q"] = query.strip()
    planned_path = (
        _zotero_api_path(
            library_type=library_type,
            library_id=library_id or "configured_library_id",
            collection_key=collection_key,
            item_key=item_key,
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
