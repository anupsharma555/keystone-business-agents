"""Read-only tools for RSS and preprint announcement history."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal
from urllib import parse, request

from keystone_agents.context_env import context_env_value, resolve_context_env_value
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
    live_rss_slack: bool = False,
    store: SQLiteStore | None = None,
) -> dict[str, Any]:
    """Return bounded canonical RSS/preprint history from local application data."""

    source = _source_filter(kind)
    try:
        active_store = store or SQLiteStore(database_url or database_url_from_env())
        items = active_store.retrieve_announcement_feed_items(
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

    linked_preprint_items: list[dict[str, Any]] = []
    linked_diagnostics: list[dict[str, str]] = []
    if kind == "preprints" and not items and selected_only is not True:
        linked_preprint_items, linked_diagnostics = _linked_preprint_discovery_items(
            query=query,
            limit=_bounded_limit(limit),
        )

    linked_rss_items: list[dict[str, Any]] = []
    linked_rss_diagnostics: list[dict[str, str]] = []
    if kind == "rss" and not items and live_rss_slack:
        linked_rss_items, linked_rss_diagnostics = _live_slack_rss_items(
            query=query,
            limit=_bounded_limit(limit),
        )

    return {
        "status": "success",
        "kind": kind,
        "query": query,
        "selected_only": selected_only,
        "item_count": len(items) + len(linked_preprint_items) + len(linked_rss_items),
        "items": (
            [_item_payload(item) for item in items]
            + linked_preprint_items
            + linked_rss_items
        ),
        "diagnostics": [
            {
                "key": "database_url_source",
                "value": "database_url_arg" if database_url else "database_url_from_env",
                "note": "Canonical announcement feed records are local application data.",
            },
            *linked_diagnostics,
            *linked_rss_diagnostics,
        ],
        "blockers": (
            []
            if items or linked_preprint_items or linked_rss_items
            else ["No matching announcement feed history was found."]
        ),
        "send_enabled": False,
    }


def _linked_preprint_discovery_items(
    *,
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    resolution = resolve_context_env_value("DISCOVERY_STORE_PATH")
    if not resolution.value:
        return [], []
    path = Path(resolution.value).expanduser()
    if not path.is_absolute() and resolution.source_repo is not None:
        path = resolution.source_repo / path
    if not path.is_file():
        return [], [
            {
                "key": "linked_discovery_store",
                "value": "unavailable",
                "note": "Configured discovery store path is not a readable file.",
            }
        ]

    normalized_query = " ".join(str(query or "").split())
    like_query = f"%{normalized_query}%"
    sql = (
        "SELECT candidate_id, source, source_item_id, title, summary, published, "
        "url, topics_json, status, metadata_json "
        "FROM discovery_candidates WHERE candidate_type = 'preprint' "
    )
    params: list[Any] = []
    if normalized_query:
        sql += "AND (title LIKE ? OR summary LIKE ? OR topics_json LIKE ?) "
        params.extend([like_query, like_query, like_query])
    sql += "ORDER BY published DESC, last_seen_at DESC LIMIT ?"
    params.append(limit)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, params).fetchall()
    except (sqlite3.Error, OSError) as exc:
        return [], [
            {
                "key": "linked_discovery_store",
                "value": "read_error",
                "note": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        ]

    payloads = [_linked_preprint_payload(dict(row)) for row in rows]
    return payloads, [
        {
            "key": "linked_discovery_store",
            "value": resolution.source,
            "note": (
                "Read persisted preprint candidates from the configured Keystone "
                "discovery store in SQLite read-only mode."
            ),
        }
    ]


def _linked_preprint_payload(row: dict[str, Any]) -> dict[str, Any]:
    topics = _json_list(row.get("topics_json"))
    metadata = _json_dict(row.get("metadata_json"))
    publication_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in (
                metadata.get("doi"),
                metadata.get("arxiv_id"),
                metadata.get("biorxiv_id"),
                metadata.get("medrxiv_id"),
                row.get("source_item_id"),
            )
            if str(value or "").strip()
        )
    )
    candidate_id = str(row.get("candidate_id") or "").strip()
    source = str(row.get("source") or "").strip()
    published = str(row.get("published") or "").strip()
    url = str(row.get("url") or "").strip()
    summary = str(row.get("summary") or "").strip()
    return {
        "feed_item_id": candidate_id,
        "title": str(row.get("title") or "").strip(),
        "url": url,
        "source": source,
        "feed": "preprints",
        "published_at": published,
        "tags": topics,
        "selected": False,
        "relevance_status": str(row.get("status") or "candidate"),
        "selection_reason": "Persisted preprint candidate in linked discovery history.",
        "summary": summary,
        "detailed_summary_seed": summary[:1400],
        "source_basis": (
            f"feed=preprints; selected=false; evidence_status=discovery_candidate; "
            f"published_at={published or 'unknown'}"
        ),
        "evidence_status": "discovery_candidate",
        "publication_ids": publication_ids,
        "evidence_notes": [],
        "slack_link": "",
        "source_ids": [value for value in (candidate_id, *publication_ids, url) if value],
    }


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item or "").strip()]


def _json_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _live_slack_rss_items(
    *,
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not _truthy(os.getenv("KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED")):
        return [], [
            {
                "key": "slack_rss_history",
                "value": "blocked",
                "note": (
                    "Live Slack RSS history requires "
                    "KEYSTONE_RSS_CONTEXT_LIVE_SLACK_READ_ENABLED=true."
                ),
            }
        ]
    token = context_env_value("SLACK_BOT_TOKEN")
    configured_channel = context_env_value("SLACK_ANNOUNCEMENTS_CHANNEL", "announcements")
    if not token or not configured_channel:
        return [], [
            {
                "key": "slack_rss_history",
                "value": "blocked",
                "note": "Slack token or announcements channel configuration is missing.",
            }
        ]
    try:
        channel_id = _slack_channel_id(token, configured_channel)
        response = _slack_api_json(
            "conversations.history",
            token=token,
            params={"channel": channel_id, "limit": min(100, max(10, limit * 4))},
        )
    except (OSError, ValueError) as exc:
        return [], [
            {
                "key": "slack_rss_history",
                "value": "read_error",
                "note": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        ]

    query_terms = [
        term
        for term in re.findall(r"[a-z0-9]+", str(query or "").lower())
        if len(term) >= 3
        and term not in {"rss", "recent", "latest", "announcement", "announcements"}
    ]
    items: list[dict[str, Any]] = []
    for message in response.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_ts = str(message.get("ts") or "").strip()
        for item in _rss_digest_items_from_slack_text(
            str(message.get("text") or ""),
            channel_id=channel_id,
            message_ts=message_ts,
        ):
            searchable = " ".join(
                str(item.get(key) or "")
                for key in ("title", "source", "tags", "summary")
            ).lower()
            if query_terms and not any(term in searchable for term in query_terms):
                continue
            items.append(item)
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    return items, [
        {
            "key": "slack_rss_history",
            "value": "live_read",
            "note": (
                "Read bounded #announcements digest history through Slack conversations.history; "
                "no message was posted or modified."
            ),
        }
    ]


def _slack_channel_id(token: str, configured_channel: str) -> str:
    clean = configured_channel.strip().lstrip("#")
    if re.fullmatch(r"[CGD][A-Z0-9]+", clean):
        return clean
    response = _slack_api_json(
        "conversations.list",
        token=token,
        params={"types": "public_channel,private_channel", "limit": 200},
    )
    for channel in response.get("channels") or []:
        if isinstance(channel, dict) and str(channel.get("name") or "") == clean:
            channel_id = str(channel.get("id") or "").strip()
            if channel_id:
                return channel_id
    raise ValueError("Configured Slack announcements channel was not found.")


def _slack_api_json(endpoint: str, *, token: str, params: dict[str, Any]) -> dict[str, Any]:
    url = "https://slack.com/api/" + endpoint + "?" + parse.urlencode(params)
    api_request = request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "keystone-rss-context/1"},
    )
    with request.urlopen(api_request, timeout=20) as response:  # noqa: S310 - fixed Slack host
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = payload.get("error") if isinstance(payload, dict) else "invalid_response"
        raise ValueError(f"Slack API request failed: {error}")
    return payload


_RSS_DIGEST_ITEM_RE = re.compile(
    r"\*Title:\*\s*(?:<(?P<title_url>https?://[^|>]+)\|(?P<title_linked>[^>]+)>|(?P<title_plain>[^\n]+))"
    r"[\s\S]*?\*Date:\*\s*(?P<date>[^\n]+)"
    r"[\s\S]*?\*Feed source:\*\s*(?P<source>[^\n]+)"
    r"[\s\S]*?\*Category:\*\s*(?P<category>[^\n]+)"
    r"[\s\S]*?\*Link:\*\s*<(?P<link>https?://[^>|]+)(?:\|[^>]+)?>"
    r"[\s\S]*?Why relevant:\s*(?P<relevance>[^\n]+)",
    flags=re.I,
)


def _rss_digest_items_from_slack_text(
    text: str,
    *,
    channel_id: str,
    message_ts: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, match in enumerate(_RSS_DIGEST_ITEM_RE.finditer(text), start=1):
        title = str(match.group("title_linked") or match.group("title_plain") or "").strip()
        url = str(match.group("link") or match.group("title_url") or "").strip()
        source = str(match.group("source") or "").strip()
        published = str(match.group("date") or "").strip()
        category = str(match.group("category") or "").strip()
        relevance = str(match.group("relevance") or "").strip()
        item_id = f"slack:{channel_id}:{message_ts}:{index}"
        items.append(
            {
                "feed_item_id": item_id,
                "title": title,
                "url": url,
                "source": source,
                "feed": "rss",
                "published_at": published,
                "tags": [category] if category else [],
                "selected": True,
                "relevance_status": "selected_digest_item",
                "selection_reason": relevance,
                "summary": relevance,
                "detailed_summary_seed": relevance[:1400],
                "source_basis": (
                    "feed=rss; selected=true; evidence_status=slack_digest_history; "
                    f"published_at={published or 'unknown'}"
                ),
                "evidence_status": "slack_digest_history",
                "publication_ids": [],
                "evidence_notes": [],
                "slack_link": "",
                "source_ids": [item_id, url],
            }
        )
    return items


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def retrieve_rss_announcement_history_impl(
    query: str = "",
    *,
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
    live: bool = False,
    store: SQLiteStore | None = None,
) -> dict[str, Any]:
    """Return historical #announcements/RSS records from local application data."""

    return retrieve_announcement_feed_history_impl(
        query=query,
        kind="rss",
        selected_only=selected_only,
        limit=limit,
        database_url=database_url,
        live_rss_slack=live,
        store=store,
    )


def retrieve_preprint_announcement_history_impl(
    query: str = "",
    *,
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
    store: SQLiteStore | None = None,
) -> dict[str, Any]:
    """Return historical preprint/#knowledge-hub records from local application data."""

    return retrieve_announcement_feed_history_impl(
        query=query,
        kind="preprints",
        selected_only=selected_only,
        limit=limit,
        database_url=database_url,
        store=store,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_rss_announcement_history(
    query: str = "",
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    live: bool = False,
) -> str:
    """Retrieve read-only RSS/#announcements history from local application data."""

    return json.dumps(
        retrieve_rss_announcement_history_impl(
            query=query,
            selected_only=selected_only,
            limit=limit,
            live=live,
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
