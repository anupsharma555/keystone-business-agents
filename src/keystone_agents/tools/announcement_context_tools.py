"""Read-only tools for RSS and preprint announcement history."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib import parse, request

from keystone_agents.context_env import context_env_value, resolve_context_env_value
from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.schemas.announcement_feed import AnnouncementFeedItem
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

DEFAULT_HISTORY_LIMIT = 8
MAX_LINKED_QUERY_TERMS = 25
MAX_EVIDENCE_INDEX_ITEMS = 12
MAX_EVIDENCE_INDEX_CHARS = 4_000
MAX_EVIDENCE_READ_CHARS = 8_000
HistoryScope = Literal["discovery", "selected"]
_LINKED_QUERY_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "should",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
    }
)
_LINKED_FIELD_PREFIX_RE = re.compile(
    r"(?<![a-z0-9_])(?P<field>"
    r"id|candidate|source|doi|url|date|after|before|on_or_after|on_or_before"
    r"):",
    re.IGNORECASE,
)
_LINKED_URL_RE = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)
_LINKED_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_LINKED_CANDIDATE_ID_RE = re.compile(
    r"(?<![a-z0-9_])[a-z0-9_.-]+:[a-z0-9_.-]+:[^\s,;]+",
    re.IGNORECASE,
)
_LINKED_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_LINKED_NATURAL_DATE_RELATION_RE = re.compile(
    r"(?<![a-z0-9_])(?P<operator>after|since|before|until|on)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})(?!\d)",
    re.IGNORECASE,
)


def _bounded_limit(value: int | None) -> int:
    return max(1, min(25, int(value or DEFAULT_HISTORY_LIMIT)))


def _source_filter(kind: Literal["rss", "preprints"]) -> str:
    return "preprints" if kind == "preprints" else ""


def _linked_text_terms(value: object) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(re.findall(r"[a-z0-9]+", str(value or "").lower()))
    )


def _clean_linked_filter_value(value: object) -> str:
    return str(value or "").strip().strip("\"'<> ").rstrip(".,;!?")


def _linked_explicit_field_filters(
    text: str,
) -> tuple[list[dict[str, str]], list[tuple[int, int]], list[dict[str, str]]]:
    filters: list[dict[str, str]] = []
    spans: list[tuple[int, int]] = []
    malformed: list[dict[str, str]] = []
    for match in _LINKED_FIELD_PREFIX_RE.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        field = str(match.group("field") or "").lower()
        value_start = match.end()
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text):
            malformed.append({"field": field, "raw": text[match.start() :]})
            spans.append((match.start(), len(text)))
            continue
        quote = text[value_start] if text[value_start] in {'"', "'"} else ""
        if quote:
            cursor = value_start + 1
            value_chars: list[str] = []
            escaped = False
            while cursor < len(text):
                character = text[cursor]
                if escaped:
                    value_chars.append(character)
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    break
                else:
                    value_chars.append(character)
                cursor += 1
            if cursor >= len(text) or text[cursor] != quote:
                malformed.append({"field": field, "raw": text[match.start() :]})
                spans.append((match.start(), len(text)))
                continue
            end = cursor + 1
            value = "".join(value_chars).strip()
        else:
            cursor = value_start
            while cursor < len(text) and text[cursor] not in " \t,;":
                cursor += 1
            end = cursor
            value = _clean_linked_filter_value(text[value_start:end])
        raw = text[match.start() : end].strip()
        if not value:
            malformed.append({"field": field, "raw": raw})
        else:
            filters.append({"field": field, "value": value, "raw": raw})
        spans.append((match.start(), end))
    return filters, spans, malformed


def _linked_query_contract(value: object) -> dict[str, Any]:
    text = " ".join(str(value or "").split())
    filters, spans, malformed_filters = _linked_explicit_field_filters(text)
    unsupported_filters: list[dict[str, str]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < prior_end and end > prior_start for prior_start, prior_end in spans)

    for field, pattern in (
        ("url", _LINKED_URL_RE),
        ("doi", _LINKED_DOI_RE),
        ("identifier", _LINKED_CANDIDATE_ID_RE),
    ):
        for match in pattern.finditer(text):
            if overlaps(*match.span()):
                continue
            matched_text = _clean_linked_filter_value(match.group(0))
            filters.append(
                {"field": field, "value": matched_text, "raw": matched_text}
            )
            spans.append(match.span())
    for match in _LINKED_NATURAL_DATE_RELATION_RE.finditer(text):
        if overlaps(*match.span()):
            continue
        unsupported_filters.append(
            {
                "kind": "natural_date_relation",
                "operator": str(match.group("operator") or "").lower(),
                "value": str(match.group("date") or ""),
                "raw": str(match.group(0) or ""),
            }
        )
        spans.append(match.span())
    for match in _LINKED_DATE_RE.finditer(text):
        if overlaps(*match.span()):
            continue
        unsupported_filters.append(
            {
                "kind": "bare_iso_date",
                "operator": "",
                "value": str(match.group(0) or ""),
                "raw": str(match.group(0) or ""),
            }
        )
        spans.append(match.span())

    scrubbed = list(text)
    for start, end in spans:
        scrubbed[start:end] = " " * (end - start)
    terms = tuple(
        term
        for term in _linked_text_terms("".join(scrubbed))
        if term not in _LINKED_QUERY_STOP_WORDS and len(term) >= 2
    )
    return {
        "text": text,
        "raw_terms": _linked_text_terms(text),
        "terms": terms,
        "filters": tuple(filters),
        "unsupported_filters": tuple(unsupported_filters),
        "malformed_filters": tuple(malformed_filters),
    }


def _normalize_linked_constraint_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalize_linked_doi(value: object) -> str:
    cleaned = _clean_linked_filter_value(value).casefold()
    cleaned = cleaned.removeprefix("doi:")
    parsed = parse.urlsplit(cleaned)
    if parsed.scheme in {"http", "https"} and parsed.netloc.casefold() in {
        "doi.org",
        "www.doi.org",
        "dx.doi.org",
    }:
        cleaned = parse.unquote(parsed.path.lstrip("/"))
    match = _LINKED_DOI_RE.fullmatch(cleaned)
    return match.group(0).casefold() if match else ""


def _normalize_linked_url(value: object) -> str:
    cleaned = _clean_linked_filter_value(value)
    parsed = parse.urlsplit(cleaned)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return parse.urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path,
            parsed.query,
            "",
        )
    )


def _linked_iso_date(value: object) -> str:
    candidate = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return ""


def linked_history_authority_constraint_records(
    value: object,
) -> tuple[dict[str, str], ...]:
    """Return paired, canonical constraints explicitly supplied by the operator."""

    contract = _linked_query_contract(value)
    constraints: list[dict[str, str]] = []
    for item in contract["filters"]:
        field = str(item["field"])
        raw = str(item["raw"])
        item_value = str(item["value"])
        if field in {"after", "on_or_after", "before", "on_or_before", "date"}:
            relation = {
                "after": "after",
                "on_or_after": "on_or_after",
                "before": "before",
                "on_or_before": "on_or_before",
                "date": "exact",
            }[field]
            normalized_date = _linked_iso_date(item_value)
            if normalized_date:
                constraints.append(
                    {"kind": "date", "relation": relation, "value": normalized_date}
                )
            else:
                constraints.append(
                    {"kind": "malformed", "relation": field, "value": raw.casefold()}
                )
        elif field == "source" and raw.count(":") >= 2:
            constraints.append(
                {
                    "kind": "candidate_id",
                    "relation": "exact",
                    "value": _normalize_linked_constraint_text(raw),
                }
            )
        elif field in {"id", "candidate", "identifier"}:
            constraints.append(
                {
                    "kind": "candidate_id",
                    "relation": "exact",
                    "value": _normalize_linked_constraint_text(item_value),
                }
            )
        elif field == "source":
            constraints.append(
                {
                    "kind": "source",
                    "relation": "exact",
                    "value": _normalize_linked_constraint_text(item_value),
                }
            )
        elif field == "doi":
            normalized_doi = _normalize_linked_doi(item_value)
            constraints.append(
                {
                    "kind": "doi" if normalized_doi else "malformed",
                    "relation": "exact" if normalized_doi else "doi",
                    "value": normalized_doi or raw.casefold(),
                }
            )
        elif field == "url":
            normalized_doi = _normalize_linked_doi(item_value)
            # A bare DOI resolver URL is a precise publication identity. Other
            # URLs become authority only when the operator explicitly used url:.
            if normalized_doi:
                constraints.append(
                    {"kind": "doi", "relation": "exact", "value": normalized_doi}
                )
            elif raw.casefold().startswith("url:"):
                normalized_url = _normalize_linked_url(item_value)
                constraints.append(
                    {
                        "kind": "url" if normalized_url else "malformed",
                        "relation": "exact" if normalized_url else "url",
                        "value": normalized_url or raw.casefold(),
                    }
                )
    for item in contract["unsupported_filters"]:
        if item["kind"] != "natural_date_relation":
            continue
        normalized_date = _linked_iso_date(item["value"])
        if not normalized_date:
            continue
        relation = {
            "after": "after",
            "since": "on_or_after",
            "before": "before",
            "until": "on_or_before",
            "on": "exact",
        }[str(item["operator"])]
        constraints.append(
            {"kind": "date", "relation": relation, "value": normalized_date}
        )
    for item in contract["malformed_filters"]:
        constraints.append(
            {
                "kind": "malformed",
                "relation": str(item["field"]),
                "value": str(item["raw"]).casefold(),
            }
        )
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in constraints:
        key = (item["kind"], item["relation"], item["value"])
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return tuple(unique)


def linked_history_authority_constraints(value: object) -> tuple[str, ...]:
    """Return stable canonical tokens for explicit operator constraints."""

    return tuple(
        f"{item['kind']}:{item['relation']}:{item['value']}"
        for item in linked_history_authority_constraint_records(value)
    )


def linked_history_candidate_constraint_values(
    candidate: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Project one retrieved candidate into exact constraint evidence fields."""

    raw_item = candidate.get("raw_item")
    raw = raw_item if isinstance(raw_item, Mapping) else candidate

    def values(*keys: str) -> list[Any]:
        observed: list[Any] = []
        for source in (candidate, raw):
            for key in keys:
                item = source.get(key)
                if isinstance(item, list | tuple):
                    observed.extend(item)
                elif item is not None and item != "":
                    observed.append(item)
        return observed

    candidate_ids = tuple(
        dict.fromkeys(
            normalized
            for item in values("candidate_id", "feed_item_id", "source_item_id")
            if (normalized := _normalize_linked_constraint_text(item))
        )
    )
    sources = tuple(
        dict.fromkeys(
            normalized
            for item in values("source")
            if (normalized := _normalize_linked_constraint_text(item))
        )
    )
    urls = tuple(
        dict.fromkeys(
            normalized
            for item in values("url")
            if (normalized := _normalize_linked_url(item))
        )
    )
    doi_inputs = [*values("publication_ids", "publication_id", "doi"), *urls]
    dois = tuple(
        dict.fromkeys(
            normalized
            for item in doi_inputs
            if (normalized := _normalize_linked_doi(item))
        )
    )
    dates = tuple(
        dict.fromkeys(
            normalized
            for item in values("published_at", "published")
            if (normalized := _linked_iso_date(item))
        )
    )
    return {
        "candidate_id": candidate_ids,
        "source": sources,
        "doi": dois,
        "url": urls,
        "date": dates,
    }


def _linked_preprint_match_score(
    title: object,
    summary: object,
    topics_json: object,
    query: object,
) -> int:
    terms = _linked_query_contract(query)["terms"]
    if not terms:
        return 0
    available: set[str] = set()
    for value in (title, summary, topics_json):
        available.update(_linked_text_terms(value))
    return sum(term in available for term in terms)


def _linked_preprint_matches_explicit_filters(
    candidate_id: object,
    source: object,
    source_item_id: object,
    published: object,
    url: object,
    metadata_json: object,
    query: object,
) -> int:
    filters = _linked_query_contract(query)["filters"]
    if not filters:
        return 1
    metadata = _json_dict(metadata_json)
    candidate = str(candidate_id or "").strip().lower()
    source_name = str(source or "").strip().lower()
    source_item = str(source_item_id or "").strip().lower()
    published_text = str(published or "").strip().lower()
    url_text = str(url or "").strip().lower()
    metadata_values = {
        str(value or "").strip().lower()
        for value in metadata.values()
        if str(value or "").strip()
    }
    all_identifiers = {
        candidate,
        source_name,
        source_item,
        published_text,
        url_text,
        *metadata_values,
    }

    for item in filters:
        field = item["field"]
        value = item["value"].strip().lower()
        raw = item["raw"].strip().lower()
        if field in {"id", "candidate"}:
            matched = value in {candidate, source_item}
        elif field == "source":
            matched = raw == candidate or value in {source_name, source_item}
        elif field == "doi":
            normalized = value.removeprefix("https://doi.org/")
            matched = normalized in {source_item, *metadata_values} or url_text == (
                f"https://doi.org/{normalized}"
            )
        elif field == "url":
            matched = value == url_text
        elif field == "date":
            matched = published_text.startswith(value)
        elif field == "after":
            matched = published_text[:10] > value
        elif field == "on_or_after":
            matched = published_text[:10] >= value
        elif field == "before":
            matched = published_text[:10] < value
        elif field == "on_or_before":
            matched = published_text[:10] <= value
        else:
            matched = value in all_identifiers
        if not matched:
            return 0
    return 1


def _canonical_json_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _feed_item_snapshot(item: AnnouncementFeedItem) -> str:
    return _canonical_json_sha256(item.model_dump(mode="json"))


def _evidence_id(evidence: object) -> str:
    payload = (
        evidence.model_dump(mode="json")
        if hasattr(evidence, "model_dump")
        else evidence
    )
    return f"evidence:{_canonical_json_sha256(payload)[:24]}"


def _evidence_index_entry(evidence: object, *, position: int) -> dict[str, Any]:
    snippet = str(getattr(evidence, "snippet", "") or "")
    source_url = str(getattr(evidence, "url", "") or "")
    metadata = getattr(evidence, "metadata", {})
    metadata_keys = (
        [str(key)[:120] for key in list(metadata)[:20]]
        if isinstance(metadata, Mapping)
        else []
    )
    return {
        "evidence_id": _evidence_id(evidence),
        "position": position,
        "kind": str(getattr(evidence, "kind", "") or "")[:80],
        "title": str(getattr(evidence, "title", "") or "")[:300],
        "url": source_url[:1200],
        "url_complete": len(source_url) <= 1200,
        "preview_only": True,
        "authoritative_metadata_in_selected_read": True,
        "source": str(getattr(evidence, "source", "") or "")[:160],
        "status": str(getattr(evidence, "status", "") or "")[:80],
        "saved_content_scope": "saved_evidence_snippet",
        "saved_content_available": bool(snippet),
        "saved_content_char_count": len(snippet),
        "reported_source_char_count": max(
            0,
            int(getattr(evidence, "char_count", 0) or 0),
        ),
        "metadata_keys": metadata_keys,
    }


def _evidence_index_page(
    item: AnnouncementFeedItem,
    *,
    start: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    used_chars = 0
    cursor = min(max(0, int(start)), len(item.evidence))
    while cursor < len(item.evidence) and len(entries) < MAX_EVIDENCE_INDEX_ITEMS:
        entry = _evidence_index_entry(item.evidence[cursor], position=cursor + 1)
        entry_chars = len(
            json.dumps(entry, ensure_ascii=True, sort_keys=True, default=str)
        )
        if entries and used_chars + entry_chars > MAX_EVIDENCE_INDEX_CHARS:
            break
        entries.append(entry)
        used_chars += entry_chars
        cursor += 1
    return entries, cursor


def _evidence_index_payload(
    item: AnnouncementFeedItem,
    *,
    start: int = 0,
) -> dict[str, Any]:
    entries, end = _evidence_index_page(item, start=start)
    full_count = len(item.evidence)
    has_more = end < full_count
    snapshot = _feed_item_snapshot(item)
    return {
        "entries": entries,
        "coverage": {
            "unit": "saved_evidence_record",
            "start": min(max(0, int(start)), full_count),
            "end": end,
            "full_count": full_count,
            "has_more": has_more,
            "complete": not has_more and start == 0,
            "next_request": (
                {
                    "feed_item_id": item.canonical_key,
                    "evidence_index_start": end,
                    "expected_snapshot_sha256": snapshot,
                }
                if has_more
                else None
            ),
        },
    }


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
    evidence_index = _evidence_index_payload(item)
    source_snapshot = _feed_item_snapshot(item)
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
        "evidence_notes_scope": "bounded_discovery_preview",
        "evidence_count": len(item.evidence),
        "evidence_index": evidence_index["entries"],
        "evidence_index_coverage": evidence_index["coverage"],
        "selected_evidence_read_required": bool(item.evidence),
        "saved_content_scope": "saved_evidence_snippet",
        "article_full_text_verified": False,
        "source_snapshot": {
            "sha256": source_snapshot,
            "basis": "canonical_saved_feed_item_and_ordered_evidence",
        },
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


def _feed_item_matches_kind(
    item: AnnouncementFeedItem,
    kind: Literal["rss", "preprints"],
) -> bool:
    source_text = " ".join([item.source, item.feed, *item.tags]).lower()
    is_preprint = "preprint" in source_text or item.feed.lower() == "preprints"
    return is_preprint if kind == "preprints" else not is_preprint


def read_announcement_feed_evidence_impl(
    feed_item_id: str,
    *,
    kind: Literal["rss", "preprints"],
    evidence_id: str = "",
    evidence_index_start: int = 0,
    start_char: int = 0,
    metadata_start: int = 0,
    max_chars: int = 4_000,
    expected_snapshot_sha256: str = "",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Read one bounded saved evidence record or a later evidence-index page."""

    item_id = str(feed_item_id or "").strip()
    if not item_id:
        raise ValueError("feed_item_id is required for a saved evidence read.")
    selected_evidence_id = str(evidence_id or "").strip()
    bounded_index_start = int(evidence_index_start or 0)
    bounded_start = int(start_char or 0)
    bounded_metadata_start = int(metadata_start or 0)
    bounded_chars = min(max(int(max_chars or 4_000), 500), MAX_EVIDENCE_READ_CHARS)
    if bounded_index_start < 0 or bounded_index_start > 100_000:
        raise ValueError("evidence_index_start must be between 0 and 100000.")
    if bounded_start < 0 or bounded_start > 10_000_000:
        raise ValueError("start_char must be between 0 and 10000000.")
    if bounded_metadata_start < 0 or bounded_metadata_start > 10_000_000:
        raise ValueError("metadata_start must be between 0 and 10000000.")
    expected_snapshot = str(expected_snapshot_sha256 or "").strip().lower()
    if expected_snapshot and not re.fullmatch(r"[0-9a-f]{64}", expected_snapshot):
        raise ValueError(
            "expected_snapshot_sha256 must be a 64-character SHA-256 hex digest."
        )
    if (bounded_index_start or bounded_start or bounded_metadata_start) and not expected_snapshot:
        return {
            "status": "continuation_identity_required",
            "operation": "read_saved_announcement_evidence",
            "kind": kind,
            "feed_item_id": item_id,
            "text": "",
            "limitations": [
                "A nonzero evidence or character cursor requires the exact saved-item "
                "snapshot digest returned by the prior read."
            ],
            "send_enabled": False,
        }

    if kind == "preprints" and os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
        from keystone_agents.canary_acceptance import public_preprint_snapshot_binding

        if public_preprint_snapshot_binding() is not None:
            return {
                "status": "source_scope_mismatch",
                "operation": "read_saved_announcement_evidence",
                "kind": kind,
                "feed_item_id": item_id,
                "text": "",
                "metadata_text": "",
                "limitations": [
                    "The active acceptance profile is bound to a reviewed public "
                    "preprint snapshot. Canonical application-database evidence reads "
                    "cannot be substituted for that source."
                ],
                "send_enabled": False,
            }

    resolved_database_url = database_url or database_url_from_env()
    try:
        item = SQLiteStore(resolved_database_url).get_announcement_feed_item(item_id)
    except Exception as exc:
        return {
            "status": "blocked",
            "operation": "read_saved_announcement_evidence",
            "kind": kind,
            "feed_item_id": item_id,
            "text": "",
            "limitations": [
                "Saved announcement evidence could not be read from local application data."
            ],
            "diagnostics": [
                {
                    "key": "storage_error",
                    "value": type(exc).__name__,
                    "note": str(exc)[:500],
                }
            ],
            "send_enabled": False,
        }
    if item is None:
        return {
            "status": "not_found",
            "operation": "read_saved_announcement_evidence",
            "kind": kind,
            "feed_item_id": item_id,
            "text": "",
            "limitations": ["No saved announcement item matched the exact feed_item_id."],
            "send_enabled": False,
        }
    if not _feed_item_matches_kind(item, kind):
        return {
            "status": "source_scope_mismatch",
            "operation": "read_saved_announcement_evidence",
            "kind": kind,
            "feed_item_id": item_id,
            "text": "",
            "limitations": [
                "The saved item exists but is outside the requested RSS/preprints scope."
            ],
            "send_enabled": False,
        }

    snapshot = _feed_item_snapshot(item)
    source_snapshot = {
        "sha256": snapshot,
        "basis": "canonical_saved_feed_item_and_ordered_evidence",
        "expected_sha256": expected_snapshot,
        "matches_expected": snapshot == expected_snapshot if expected_snapshot else None,
    }
    if expected_snapshot and snapshot != expected_snapshot:
        return {
            "status": "source_changed",
            "operation": "read_saved_announcement_evidence",
            "kind": kind,
            "feed_item_id": item_id,
            "source_snapshot": source_snapshot,
            "text": "",
            "limitations": [
                "The saved announcement item changed before this bounded continuation; "
                "content from different snapshots was not mixed."
            ],
            "send_enabled": False,
        }

    index_payload = _evidence_index_payload(item, start=bounded_index_start)
    common = {
        "operation": "read_saved_announcement_evidence",
        "kind": kind,
        "feed_item_id": item.canonical_key,
        "feed_item": {
            "title": item.title,
            "url": item.canonical_url or item.url,
            "source": item.source,
            "feed": item.feed,
            "published_at": item.published_at,
            "publication_ids": _publication_ids(item),
        },
        "source_snapshot": source_snapshot,
        "evidence_index": index_payload["entries"],
        "evidence_index_coverage": index_payload["coverage"],
        "saved_content_scope": "saved_evidence_snippet",
        "article_full_text_verified": False,
        "send_enabled": False,
    }
    if not selected_evidence_id:
        return {
            **common,
            "status": "success",
            "read_mode": "evidence_index",
            "text": "",
            "content_status": "not_requested",
            "content_complete": False,
            "limitations": [
                "This response lists bounded saved-evidence identities only; request "
                "one exact evidence_id to inspect its saved content."
            ],
            "continuation": {
                "available": bool(index_payload["coverage"]["has_more"]),
                "next_request": index_payload["coverage"]["next_request"],
            },
        }

    matched_position = -1
    matched_evidence = None
    for position, evidence in enumerate(item.evidence):
        if _evidence_id(evidence) == selected_evidence_id:
            matched_position = position
            matched_evidence = evidence
            break
    if matched_evidence is None:
        return {
            **common,
            "status": "not_found",
            "read_mode": "evidence_content",
            "evidence_id": selected_evidence_id,
            "text": "",
            "content_status": "evidence_identity_not_found",
            "content_complete": False,
            "limitations": [
                "The exact evidence_id was not present in the saved item snapshot."
            ],
            "continuation": {"available": False, "next_request": None},
        }

    full_text = str(matched_evidence.snippet or "")
    full_metadata_text = json.dumps(
        {
            "evidence_id": selected_evidence_id,
            "position": matched_position + 1,
            "kind": matched_evidence.kind,
            "title": matched_evidence.title,
            "url": matched_evidence.url,
            "source": matched_evidence.source,
            "status": matched_evidence.status,
            "reported_source_char_count": max(
                0,
                int(matched_evidence.char_count or 0),
            ),
            "metadata": matched_evidence.metadata,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    window_start = min(bounded_start, len(full_text))
    window_end = min(len(full_text), window_start + bounded_chars)
    has_more = window_end < len(full_text)
    text = full_text[window_start:window_end]
    metadata_window_start = min(bounded_metadata_start, len(full_metadata_text))
    metadata_window_end = min(
        len(full_metadata_text),
        metadata_window_start + bounded_chars,
    )
    metadata_has_more = metadata_window_end < len(full_metadata_text)
    metadata_text = full_metadata_text[metadata_window_start:metadata_window_end]
    limitations: list[str] = []
    if not full_text:
        content_status = (
            "empty_saved_record"
            if str(matched_evidence.status or "").lower() == "success"
            else "unavailable_saved_record"
        )
        limitations.append(
            "The saved evidence record contains no content text; source absence was not "
            "inferred from a discovery preview."
        )
    else:
        content_status = "saved_content_available"
    if has_more:
        limitations.append(
            f"Saved evidence exceeded this {bounded_chars}-character response window."
        )
    if metadata_has_more:
        limitations.append(
            f"Authoritative evidence metadata exceeded this {bounded_chars}-character "
            "metadata response window."
        )
    if not _evidence_index_entry(
        matched_evidence,
        position=matched_position + 1,
    )["url_complete"]:
        limitations.append(
            "The evidence-index URL is a discovery preview. The exact URL is in the "
            "bounded authoritative metadata stream and may require continuation."
        )
    if matched_evidence.kind == "article":
        limitations.append(
            "The record is article-kind evidence, but only its saved evidence snippet "
            "is proven available; full-article coverage is not inferred."
        )
    continuation_available = has_more or metadata_has_more
    next_request = (
        {
            "feed_item_id": item.canonical_key,
            "evidence_id": selected_evidence_id,
            "start_char": window_end,
            "metadata_start": metadata_window_end,
            "max_chars": bounded_chars,
            "expected_snapshot_sha256": snapshot,
        }
        if continuation_available
        else None
    )
    return {
        **common,
        "status": "success",
        "read_mode": "evidence_content",
        "evidence": {
            **_evidence_index_entry(
                matched_evidence,
                position=matched_position + 1,
            ),
        },
        "evidence_id": selected_evidence_id,
        "text": text,
        "metadata_text": metadata_text,
        "metadata_format": "canonical_json",
        "content_status": content_status,
        "content_complete": bool(full_text) and not has_more,
        "record_complete": not has_more and not metadata_has_more,
        "read_window": {
            "unit": "saved_evidence_unicode_characters",
            "start_char": window_start,
            "end_char": window_end,
            "full_char_count": len(full_text),
            "has_more": has_more,
            "part_id": (
                f"announcement-evidence:{item.canonical_key}:{selected_evidence_id}:"
                f"snapshot:{snapshot}:chars:{window_start}-{window_end}"
            ),
        },
        "metadata_window": {
            "unit": "authoritative_evidence_metadata_unicode_characters",
            "start_char": metadata_window_start,
            "end_char": metadata_window_end,
            "full_char_count": len(full_metadata_text),
            "has_more": metadata_has_more,
            "part_id": (
                f"announcement-evidence:{item.canonical_key}:{selected_evidence_id}:"
                f"snapshot:{snapshot}:metadata:{metadata_window_start}-"
                f"{metadata_window_end}"
            ),
        },
        "limitations": limitations,
        "continuation": {
            "available": continuation_available,
            "next_request": next_request,
        },
    }


def retrieve_announcement_feed_history_impl(
    query: str = "",
    *,
    kind: Literal["rss", "preprints"] = "rss",
    selected_only: bool | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    database_url: str | None = None,
    live_rss_slack: bool = False,
) -> dict[str, Any]:
    """Return bounded canonical RSS/preprint history from local application data."""

    if kind == "preprints" and os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
        from keystone_agents.canary_acceptance import public_preprint_snapshot_binding

        binding = public_preprint_snapshot_binding()
        if binding is not None:
            source_path, source_sha256 = binding
            if selected_only is True:
                public_items: list[dict[str, Any]] = []
                public_diagnostics = [
                    {
                        "key": "canary_public_preprint_snapshot",
                        "value": source_sha256,
                        "note": "Validated the exact profile-bound public snapshot.",
                    }
                ]
            else:
                public_items, public_diagnostics = _linked_preprint_discovery_items(
                    query=query,
                    limit=_bounded_limit(limit),
                    source_path=source_path,
                    source_sha256=source_sha256,
                )
            return {
                "status": "success",
                "kind": kind,
                "query": query,
                "selected_only": selected_only,
                "item_count": len(public_items),
                "items": public_items,
                "diagnostics": [
                    {
                        "key": "database_url_source",
                        "value": "bypassed_by_public_snapshot",
                        "note": (
                            "The public acceptance scenario reads only its reviewed "
                            "snapshot, never canonical or operator application data."
                        ),
                    },
                    *public_diagnostics,
                ],
                "blockers": (
                    []
                    if public_items
                    else ["No matching announcement feed history was found."]
                ),
                "send_enabled": False,
            }

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
            *(
                [
                    {
                        "key": "canonical_history_retrieval_policy",
                        "value": "fts_plus_saved_evidence_ranked_before_limit",
                        "note": (
                            "Bounded canonical recall combines the derived item index "
                            "with saved evidence records, ranks distinct candidates by "
                            "matched query terms, then applies the requested result limit. "
                            "Legacy indexes are read compatibly and are not rewritten."
                        ),
                    }
                ]
                if items
                else []
            ),
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
    source_path: Path | None = None,
    source_sha256: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if source_path is not None:
        path = source_path
        resolution_source = "acceptance_profile"
    else:
        resolution = resolve_context_env_value("DISCOVERY_STORE_PATH")
        if not resolution.value:
            return [], []
        path = Path(resolution.value).expanduser()
        if not path.is_absolute() and resolution.source_repo is not None:
            path = resolution.source_repo / path
        resolution_source = resolution.source
    if not path.is_file():
        return [], [
            {
                "key": "linked_discovery_store",
                "value": "unavailable",
                "note": "Configured discovery store path is not a readable file.",
            }
        ]

    normalized_query = " ".join(str(query or "").split())
    query_contract = _linked_query_contract(normalized_query)
    raw_query_terms = tuple(query_contract["raw_terms"])
    query_terms = tuple(query_contract["terms"])
    explicit_filters = tuple(query_contract["filters"])
    unsupported_filters = tuple(query_contract["unsupported_filters"])
    malformed_filters = tuple(query_contract["malformed_filters"])
    if malformed_filters:
        return [], [
            {
                "key": "linked_discovery_query",
                "value": "malformed_explicit_filter",
                "note": (
                    "An explicit filter had an empty or unclosed quoted value. "
                    "No saved-history rows were read; correct the filter syntax."
                ),
            }
        ]
    if (
        normalized_query
        and not query_terms
        and not explicit_filters
        and unsupported_filters
    ):
        return [], [
            {
                "key": "linked_discovery_query",
                "value": "unsupported_filter_syntax",
                "note": (
                    "A bare ISO date or natural-language date relation is not an "
                    "exact filter. Use date:, after:, on_or_after:, before:, or "
                    "on_or_before: while preserving the operator's constraint."
                ),
            }
        ]
    if normalized_query and not query_terms and not explicit_filters:
        return [], [
            {
                "key": "linked_discovery_query",
                "value": "no_search_terms",
                "note": (
                    "The linked preprint query contained no bounded topical terms "
                    "or explicit filters."
                ),
            }
        ]
    if len(raw_query_terms) > MAX_LINKED_QUERY_TERMS:
        return [], [
            {
                "key": "linked_discovery_query",
                "value": "too_many_terms",
                "note": (
                    "The linked preprint query exceeded the bounded term limit; "
                    "no requested terms were dropped."
                ),
            }
        ]
    score_sql = (
        "linked_preprint_match_score(title, summary, topics_json, ?)"
        if query_terms
        else "0"
    )
    sql = (
        "SELECT candidate_id, source, source_item_id, title, summary, published, "
        f"url, topics_json, status, metadata_json, {score_sql} AS match_score "
        "FROM discovery_candidates WHERE candidate_type = 'preprint' "
    )
    params: list[Any] = [normalized_query] if query_terms else []
    if explicit_filters:
        sql += (
            "AND linked_preprint_matches_explicit_filters("
            "candidate_id, source, source_item_id, published, url, metadata_json, ?"
            ") = 1 "
        )
        params.append(normalized_query)
    # Canonical announcement search uses FTS OR for broad recall. The linked
    # fallback now uses bounded whole-token recall and ranks by the number of
    # matching query terms. Unmatched task wording is reported in diagnostics
    # rather than being treated as an implicit exact constraint. Explicit
    # identity/source/date filters remain strict.
    if query_terms:
        sql += "AND linked_preprint_match_score(title, summary, topics_json, ?) > 0 "
        params.append(normalized_query)
    sql += "ORDER BY match_score DESC, published DESC, last_seen_at DESC LIMIT ?"
    params.append(_bounded_limit(limit))
    canary_snapshot_sha256 = source_sha256
    if (
        os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE")
        and not canary_snapshot_sha256
    ):
        from keystone_agents.canary_acceptance import public_preprint_snapshot_guard

        canary_snapshot_sha256 = public_preprint_snapshot_guard(path)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            connection.create_function(
                "linked_preprint_match_score",
                4,
                _linked_preprint_match_score,
                deterministic=True,
            )
            connection.create_function(
                "linked_preprint_matches_explicit_filters",
                7,
                _linked_preprint_matches_explicit_filters,
                deterministic=True,
            )
            connection.row_factory = sqlite3.Row
            inventory_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM discovery_candidates "
                    "WHERE candidate_type = 'preprint'"
                ).fetchone()[0]
            )
            rows = connection.execute(sql, params).fetchall()
            matched_terms = [
                term
                for term in query_terms
                if connection.execute(
                    "SELECT 1 FROM discovery_candidates "
                    "WHERE candidate_type = 'preprint' "
                    "AND linked_preprint_match_score(title, summary, topics_json, ?) > 0 "
                    "LIMIT 1",
                    (term,),
                ).fetchone()
                is not None
            ]
            unmatched_terms = [
                term for term in query_terms if term not in matched_terms
            ]
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
            "value": resolution_source,
            "note": (
                "Read persisted preprint candidates from the configured Keystone "
                "discovery store in SQLite read-only mode."
            ),
        },
        {
            "key": "linked_discovery_query_policy",
            "value": "ranked_whole_token_any",
            "note": (
                "Candidates require at least one whole topical-term match and are "
                "ranked by matched-term count. The model owns relevance selection; "
                "explicit identity/source/date filters remain mandatory."
            ),
        },
        {
            "key": "linked_discovery_inventory",
            "value": str(inventory_count),
            "note": "Count of preprint candidates in the authorized read-only store.",
        },
        *(
            [
                {
                    "key": "linked_discovery_query_coverage",
                    "value": (
                        f"matched={','.join(matched_terms) or 'none'}; "
                        "unmatched="
                        f"{','.join(unmatched_terms) or 'none'}"
                    ),
                    "note": (
                        "Coverage is diagnostic only; the model still owns relevance "
                        "and selection over returned candidates."
                    ),
                }
            ]
            if query_terms
            else []
        ),
        *(
            [
                {
                    "key": "linked_discovery_explicit_filters",
                    "value": str(len(explicit_filters)),
                    "note": "All explicit identity/source/date filters were enforced.",
                }
            ]
            if explicit_filters
            else []
        ),
        *(
            [
                {
                    "key": "linked_discovery_unsupported_filters",
                    "value": str(len(unsupported_filters)),
                    "note": (
                        "Bare ISO dates and natural-language date relations were not "
                        "silently enforced as exact-day filters. Candidate published_at "
                        "values remain visible for model review or typed reformulation."
                    ),
                }
            ]
            if unsupported_filters
            else []
        ),
        *(
            [
                {
                    "key": "canary_public_preprint_snapshot",
                    "value": canary_snapshot_sha256,
                    "note": "Read the exact profile-bound public snapshot.",
                }
            ]
            if canary_snapshot_sha256
            else []
        ),
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
) -> dict[str, Any]:
    """Return historical #announcements/RSS records from local application data."""

    return retrieve_announcement_feed_history_impl(
        query=query,
        kind="rss",
        selected_only=selected_only,
        limit=limit,
        database_url=database_url,
        live_rss_slack=live,
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


def read_rss_announcement_evidence_impl(
    feed_item_id: str,
    *,
    evidence_id: str = "",
    evidence_index_start: int = 0,
    start_char: int = 0,
    metadata_start: int = 0,
    max_chars: int = 4_000,
    expected_snapshot_sha256: str = "",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Read bounded saved evidence for one exact canonical RSS item."""

    return read_announcement_feed_evidence_impl(
        feed_item_id,
        kind="rss",
        evidence_id=evidence_id,
        evidence_index_start=evidence_index_start,
        start_char=start_char,
        metadata_start=metadata_start,
        max_chars=max_chars,
        expected_snapshot_sha256=expected_snapshot_sha256,
        database_url=database_url,
    )


def read_preprint_announcement_evidence_impl(
    feed_item_id: str,
    *,
    evidence_id: str = "",
    evidence_index_start: int = 0,
    start_char: int = 0,
    metadata_start: int = 0,
    max_chars: int = 4_000,
    expected_snapshot_sha256: str = "",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Read bounded saved evidence for one exact canonical preprint item."""

    return read_announcement_feed_evidence_impl(
        feed_item_id,
        kind="preprints",
        evidence_id=evidence_id,
        evidence_index_start=evidence_index_start,
        start_char=start_char,
        metadata_start=metadata_start,
        max_chars=max_chars,
        expected_snapshot_sha256=expected_snapshot_sha256,
        database_url=database_url,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_rss_announcement_history(
    query: str = "",
    history_scope: HistoryScope = "discovery",
    limit: int = DEFAULT_HISTORY_LIMIT,
    live: bool = False,
) -> str:
    """Retrieve read-only RSS/#announcements history from local application data.

    Keep ``query`` limited to source-relevant topic, entity, method, date, or
    identifier constraints. Use ``history_scope="discovery"`` for the bounded
    candidate history and ``history_scope="selected"`` only when the operator
    explicitly asks for previously selected items. Live Slack fallback remains
    separately gated.

    Discovery results contain a bounded ``evidence_index``. Use the separate
    ``read_rss_announcement_evidence`` tool to inspect exact saved evidence.
    """

    return json.dumps(
        retrieve_rss_announcement_history_impl(
            query=query,
            selected_only=True if history_scope == "selected" else None,
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
    history_scope: HistoryScope = "discovery",
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> str:
    """Retrieve read-only preprint/#knowledge-hub history from local application data.

    Use ``history_scope="discovery"`` for the bounded candidate history. Use
    ``history_scope="selected"`` only when the operator explicitly asks for
    previously selected items; a saved public-source snapshot is not by itself
    a previously-selected-only request. Put topical, entity, method, and
    date/source constraints in ``query``. Omit
    task instructions and operator organization names unless they are actual
    source constraints. Whole topical terms are ranked for recall; explicit
    ``id:``, ``source:``, ``doi:``, ``url:``, and ``date:`` filters stay strict.
    Use ``after:``, ``on_or_after:``, ``before:``, or ``on_or_before:`` for date
    ranges. Bare ISO dates are reported as unsupported syntax instead of being
    silently treated as exact-day filters.

    Discovery results contain a bounded ``evidence_index``. Use the separate
    ``read_preprint_announcement_evidence`` tool to inspect exact saved evidence.
    """

    return json.dumps(
        retrieve_preprint_announcement_history_impl(
            query=query,
            selected_only=True if history_scope == "selected" else None,
            limit=limit,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_rss_announcement_evidence(
    feed_item_id: str,
    evidence_id: str = "",
    evidence_index_start: int = 0,
    start_char: int = 0,
    metadata_start: int = 0,
    max_chars: int = 4_000,
    expected_snapshot_sha256: str = "",
) -> str:
    """Read bounded saved evidence for one exact canonical RSS history item.

    Pass one ``evidence_id`` from the discovery index to read saved content.
    Omit it only when following an evidence-index continuation. Follow returned
    continuation arguments exactly and stop on a changed snapshot.
    """

    return json.dumps(
        read_rss_announcement_evidence_impl(
            feed_item_id,
            evidence_id=evidence_id,
            evidence_index_start=evidence_index_start,
            start_char=start_char,
            metadata_start=metadata_start,
            max_chars=max_chars,
            expected_snapshot_sha256=expected_snapshot_sha256,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_preprint_announcement_evidence(
    feed_item_id: str,
    evidence_id: str = "",
    evidence_index_start: int = 0,
    start_char: int = 0,
    metadata_start: int = 0,
    max_chars: int = 4_000,
    expected_snapshot_sha256: str = "",
) -> str:
    """Read bounded saved evidence for one exact canonical preprint history item.

    Pass one ``evidence_id`` from the discovery index to read saved content.
    Omit it only when following an evidence-index continuation. Follow returned
    continuation arguments exactly and stop on a changed snapshot.
    """

    return json.dumps(
        read_preprint_announcement_evidence_impl(
            feed_item_id,
            evidence_id=evidence_id,
            evidence_index_start=evidence_index_start,
            start_char=start_char,
            metadata_start=metadata_start,
            max_chars=max_chars,
            expected_snapshot_sha256=expected_snapshot_sha256,
        ),
        ensure_ascii=True,
        sort_keys=True,
        default=str,
    )
