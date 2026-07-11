#!/usr/bin/env python3
"""Resolve and read the latest KNIOps research note with sanitized evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from keystone_agents.config import load_settings
from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_drive_list_folder_impl,
    google_drive_search_files_impl,
)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"


def execute_research_note_read(
    *,
    live: bool,
    search_files: Callable[..., dict[str, Any]] = google_drive_search_files_impl,
    list_folder: Callable[..., dict[str, Any]] = google_drive_list_folder_impl,
    read_doc: Callable[..., dict[str, Any]] = google_doc_read_impl,
) -> dict[str, Any]:
    if not live:
        return {
            "status": "dry-run",
            "target_folder": "KNIOps/Research",
            "openai_requests": 0,
            "provider_reads": 0,
            "provider_writes": 0,
            "send_enabled": False,
        }
    root_search = search_files("research", folder_path="KNIOps", max_items=10, live=True)
    folders = [
        item
        for item in root_search.get("items") or []
        if str(item.get("name") or "").strip().casefold() == "research"
        and _mime_type(item) == GOOGLE_FOLDER_MIME
    ]
    if len(folders) != 1:
        return _blocked("Expected one exact KNIOps/Research folder.", folder_matches=len(folders))

    listing = list_folder("KNIOps/Research", max_items=50, live=True)
    documents = [item for item in listing.get("items") or [] if _mime_type(item) == GOOGLE_DOC_MIME]
    if not documents:
        return _blocked("KNIOps/Research contains no Google Docs.", folder_matches=1)
    documents.sort(key=lambda item: str(item.get("modified_time") or ""), reverse=True)
    latest_modified = str(documents[0].get("modified_time") or "")
    latest = [item for item in documents if str(item.get("modified_time") or "") == latest_modified]
    if len(latest) != 1:
        return _blocked(
            "Latest KNIOps research-note identity is ambiguous.",
            folder_matches=1,
            document_matches=len(latest),
        )
    selected = latest[0]
    result = read_doc(
        str(selected.get("id") or ""),
        folder_path="KNIOps/Research",
        max_chars=12000,
        live=True,
    )
    text = str(result.get("text") or "")
    passed = bool(
        result.get("status") == "success"
        and result.get("document_id") == selected.get("id")
        and text
        and result.get("truncated") is False
    )
    return {
        "status": "pass" if passed else "partial",
        "failure": "" if passed else "Selected research-note read verification failed.",
        "folder_matches": 1,
        "document_matches": len(documents),
        "selected_document_id_hash": _hash(result.get("document_id")),
        "selected_title_hash": _hash(result.get("title")),
        "selected_modified_time": latest_modified,
        "char_count": int(result.get("char_count") or len(text)),
        "truncated": bool(result.get("truncated")),
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "body_persisted": False,
        "openai_requests": 0,
        "provider_reads": 3,
        "provider_writes": 0,
        "send_enabled": False,
    }


def _blocked(reason: str, **evidence: Any) -> dict[str, Any]:
    return {
        "status": "blocked",
        "failure": reason,
        **evidence,
        "openai_requests": 0,
        "provider_writes": 0,
        "send_enabled": False,
    }


def _mime_type(item: dict[str, Any]) -> str:
    return str(item.get("mime_type") or item.get("mimeType") or "")


def _hash(value: object) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:12]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-google-workspace", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/workspace-research-note-read-live.json"),
    )
    args = parser.parse_args()
    load_settings(force_dotenv=True)
    if args.live_google_workspace:
        os.environ["KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS"] = "true"
    result = execute_research_note_read(live=bool(args.live_google_workspace))
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(rendered, end="")
    return 0 if result["status"] in {"pass", "dry-run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
