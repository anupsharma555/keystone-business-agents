"""SQLite lexical index and typed handoff refs for local presentation evidence."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any

from keystone_agents.schemas.work_item import WorkItemArtifactRef
from keystone_agents.tools.internal_data_tools import (
    presentation_read_local_impl,
    presentation_search_local_impl,
)


def refresh_presentation_index(
    database_path: Path,
    *,
    max_decks: int = 100,
    live: bool = False,
) -> dict[str, Any]:
    """Rebuild a bounded local-only slide index from the allowlisted library."""

    if not live:
        return {
            "status": "dry-run",
            "database_path": str(database_path),
            "deck_count": 0,
            "slide_count": 0,
            "parent_modified": False,
        }
    inventory = presentation_search_local_impl("", max_items=max_decks, live=True)
    items = [item for item in inventory.get("items", []) if isinstance(item, dict)]
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        _create_schema(connection)
        connection.execute("DELETE FROM presentation_slides")
        deck_count = 0
        slide_count = 0
        for item in items:
            relative_path = str(item.get("relative_path") or "")
            if not relative_path:
                continue
            deck = presentation_read_local_impl(relative_path, max_slides=100, live=True)
            if deck.get("status") != "success":
                continue
            deck_count += 1
            for slide in deck.get("slides", []):
                if not isinstance(slide, dict):
                    continue
                connection.execute(
                    """
                    INSERT INTO presentation_slides (
                        relative_path, deck_title, deck_sha256, modified_time,
                        slide_number, slide_id, slide_title, slide_text, speaker_notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        relative_path,
                        str(deck.get("title") or ""),
                        str(deck.get("content_sha256") or ""),
                        str(deck.get("modified_time") or ""),
                        int(slide.get("slide_number") or 0),
                        str(slide.get("slide_id") or ""),
                        str(slide.get("title") or ""),
                        str(slide.get("text") or ""),
                        str(slide.get("speaker_notes") or ""),
                    ),
                )
                slide_count += 1
        connection.commit()
    finally:
        connection.close()
    return {
        "status": "success",
        "database_path": str(database_path),
        "deck_count": deck_count,
        "slide_count": slide_count,
        "scan_truncated": bool(inventory.get("scan_truncated")),
        "parent_modified": False,
    }


def search_presentation_index(
    database_path: Path,
    query: str,
    *,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Search indexed deck titles, paths, slide text, and notes by lexical terms."""

    terms = [term.casefold() for term in re.findall(r"[a-zA-Z0-9]+", query)]
    if not terms or not database_path.is_file():
        return []
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM presentation_slides ORDER BY modified_time DESC, slide_number ASC"
        ).fetchall()
    finally:
        connection.close()
    matches: list[dict[str, Any]] = []
    for row in rows:
        haystack = " ".join(
            str(row[name] or "")
            for name in (
                "relative_path",
                "deck_title",
                "slide_title",
                "slide_text",
                "speaker_notes",
            )
        ).casefold()
        if any(term not in haystack for term in terms):
            continue
        score = sum(haystack.count(term) for term in terms)
        payload = dict(row)
        payload["lexical_score"] = score
        payload["evidence_excerpt"] = _evidence_excerpt(
            str(row["slide_text"] or ""), str(row["speaker_notes"] or ""), terms
        )
        matches.append(payload)
    matches.sort(
        key=lambda item: (
            int(item["lexical_score"]),
            str(item["modified_time"]),
            -int(item["slide_number"]),
        ),
        reverse=True,
    )
    return matches[: min(max(int(max_results), 1), 100)]


def presentation_hits_to_artifact_refs(
    hits: list[dict[str, Any]],
) -> list[WorkItemArtifactRef]:
    """Promote lexical hits into bounded typed artifacts for context packs."""

    refs: list[WorkItemArtifactRef] = []
    for hit in hits:
        relative_path = str(hit.get("relative_path") or "")
        slide_number = int(hit.get("slide_number") or 0)
        deck_sha256 = str(hit.get("deck_sha256") or "")
        artifact_id = hashlib.sha256(
            f"{relative_path}\n{deck_sha256}\n{slide_number}".encode()
        ).hexdigest()[:24]
        refs.append(
            WorkItemArtifactRef(
                artifact_type="presentation_slide_evidence",
                artifact_id=artifact_id,
                source_agent="google_workspace_context_agent",
                approval_state="approved_for_internal_context",
                title=(
                    f"{str(hit.get('deck_title') or Path(relative_path).name)} "
                    f"— slide {slide_number}"
                ),
                summary=str(hit.get("evidence_excerpt") or ""),
                selected=True,
                metadata={
                    "relative_path": relative_path,
                    "slide_number": slide_number,
                    "slide_id": str(hit.get("slide_id") or ""),
                    "deck_sha256": deck_sha256,
                    "modified_time": str(hit.get("modified_time") or ""),
                    "lexical_score": int(hit.get("lexical_score") or 0),
                    "snapshot": True,
                    "parent_modified": False,
                    "send_enabled": False,
                },
            )
        )
    return refs


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS presentation_slides (
            relative_path TEXT NOT NULL,
            deck_title TEXT NOT NULL,
            deck_sha256 TEXT NOT NULL,
            modified_time TEXT NOT NULL,
            slide_number INTEGER NOT NULL,
            slide_id TEXT NOT NULL,
            slide_title TEXT NOT NULL,
            slide_text TEXT NOT NULL,
            speaker_notes TEXT NOT NULL,
            PRIMARY KEY (relative_path, deck_sha256, slide_number)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_presentation_slides_path "
        "ON presentation_slides(relative_path)"
    )


def _evidence_excerpt(text: str, notes: str, terms: list[str]) -> str:
    combined = " ".join(part.strip() for part in (text, notes) if part.strip())
    if len(combined) <= 500:
        return combined
    lowered = combined.casefold()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max((min(positions) if positions else 0) - 120, 0)
    return combined[start : start + 500].strip()
