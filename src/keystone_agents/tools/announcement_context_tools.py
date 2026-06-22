"""Read-only tools for RSS and preprint announcement history."""

from __future__ import annotations

import json
from typing import Any, Literal

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

DEFAULT_HISTORY_LIMIT = 8


def _bounded_limit(value: int | None) -> int:
    return max(1, min(25, int(value or DEFAULT_HISTORY_LIMIT)))


def _source_filter(kind: Literal["rss", "preprints"]) -> str:
    return "preprints" if kind == "preprints" else ""


def _evidence_notes(item: AnnouncementFeedItem) -> list[str]:
    notes: list[str] = []
    for evidence in item.evidence[:3]:
        parts = [
            evidence.kind,
            evidence.title,
            evidence.status,
            evidence.snippet,
            evidence.url,
        ]
        note = " | ".join(str(part).strip() for part in parts if str(part or "").strip())
        if note:
            notes.append(note[:700])
    return notes


def _publication_ids(item: AnnouncementFeedItem) -> list[str]:
    ids = [
        item.publication_id,
        item.doi,
        item.arxiv_id,
        item.biorxiv_id,
        item.medrxiv_id,
    ]
    return list(dict.fromkeys(value for value in ids if value))


def _evidence_status(item: AnnouncementFeedItem) -> str:
    if any(
        evidence.kind == "article" and evidence.status == "success"
        for evidence in item.evidence
    ):
        return "article_extracted"
    if any(
        evidence.kind == "search" and evidence.status == "success"
        for evidence in item.evidence
    ):
        return "search_evidence_only"
    if item.summary or item.selection_reason:
        return "historical_summary_only"
    return "metadata_only"


def _source_basis(item: AnnouncementFeedItem) -> str:
    parts = [
        f"feed={item.feed or item.source or 'unknown'}",
        f"selected={item.selected}",
        f"evidence_status={_evidence_status(item)}",
    ]
    if item.published_at:
        parts.append(f"published_at={item.published_at}")
    if item.slack_link:
        parts.append("slack_link_available=true")
    return "; ".join(parts)


def _detailed_summary_seed(item: AnnouncementFeedItem) -> str:
    parts = [
        item.summary,
        item.selection_reason,
        str(item.review_metadata.get("source_snippet") or ""),
        *[evidence.snippet for evidence in item.evidence[:3]],
    ]
    text = " ".join(part.strip() for part in parts if str(part or "").strip())
    return text[:1400]


def _item_payload(item: AnnouncementFeedItem) -> dict[str, Any]:
    publication_ids = _publication_ids(item)
    return {
        "feed_item_id": item.canonical_key,
        "title": item.title,
        "url": item.canonical_url or item.url,
        "source": item.source,
        "feed": item.feed,
        "published_at": item.published_at,
        "tags": item.tags,
        "selected": item.selected,
        "relevance_status": item.relevance_status,
        "selection_reason": item.selection_reason,
        "summary": item.summary,
        "detailed_summary_seed": _detailed_summary_seed(item),
        "source_basis": _source_basis(item),
        "evidence_status": _evidence_status(item),
        "publication_ids": publication_ids,
        "evidence_notes": _evidence_notes(item),
        "slack_link": item.slack_link,
        "source_ids": [
            value
            for value in (
                item.canonical_key,
                *publication_ids,
                item.canonical_url or item.url,
            )
            if value
        ],
    }


def retrieve_announcement_feed_history_impl(
    query: str = "",
    *,
    kind: Literal["rss", "preprints"] = "rss",
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return bounded canonical RSS/preprint history from local application data."""

    resolved_database_url = database_url or database_url_from_env()
    source = _source_filter(kind)
    try:
        store = SQLiteStore(resolved_database_url)
        items = store.retrieve_announcement_feed_items(
            query=query,
            source=source or None,
            selected_only=selected_only,
            limit=_bounded_limit(limit),
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "kind": kind,
            "query": query,
            "items": [],
            "diagnostics": [
                {
                    "key": "storage_error",
                    "value": type(exc).__name__,
                    "note": str(exc)[:500],
                }
            ],
            "blockers": [
                "Announcement feed history could not be read from local application data."
            ],
            "send_enabled": False,
        }

    if kind == "rss":
        items = [
            item
            for item in items
            if "preprint" not in " ".join([item.source, item.feed, *item.tags]).lower()
        ]

    return {
        "status": "success",
        "kind": kind,
        "query": query,
        "selected_only": selected_only,
        "item_count": len(items),
        "items": [_item_payload(item) for item in items],
        "diagnostics": [
            {
                "key": "database_url_source",
                "value": "database_url_arg" if database_url else "database_url_from_env",
                "note": "Canonical announcement feed records are local application data.",
            }
        ],
        "blockers": [] if items else ["No matching announcement feed history was found."],
        "send_enabled": False,
    }


def retrieve_rss_announcement_history_impl(
    query: str = "",
    *,
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return historical #announcements/RSS records from local application data."""

    return retrieve_announcement_feed_history_impl(
        query=query,
        kind="rss",
        selected_only=selected_only,
        limit=limit,
        database_url=database_url,
    )


def retrieve_preprint_announcement_history_impl(
    query: str = "",
    *,
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return historical preprint/#knowledge-hub records from local application data."""

    return retrieve_announcement_feed_history_impl(
        query=query,
        kind="preprints",
        selected_only=selected_only,
        limit=limit,
        database_url=database_url,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_rss_announcement_history(
    query: str = "",
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> str:
    """Retrieve read-only RSS/#announcements history from local application data."""

    return json.dumps(
        retrieve_rss_announcement_history_impl(
            query=query,
            selected_only=selected_only,
            limit=limit,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_preprint_announcement_history(
    query: str = "",
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> str:
    """Retrieve read-only preprint/#knowledge-hub history from local application data."""

    return json.dumps(
        retrieve_preprint_announcement_history_impl(
            query=query,
            selected_only=selected_only,
            limit=limit,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
