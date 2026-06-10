"""Utilities for keeping source URLs visible in user-facing answers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

URL_RE = re.compile(r"https?://[^\s<>)\]]+", flags=re.I)


def source_url_entries(sources: Any, *, limit: int = 3) -> list[str]:
    """Return compact source entries with URLs from common Keystone source shapes."""

    entries: list[str] = []
    seen: set[str] = set()
    for source in _iter_sources(sources):
        url = _source_value(source, "url")
        if not url or url in seen:
            continue
        title = (
            _source_value(source, "title")
            or _source_value(source, "source_id")
            or _source_value(source, "source_type")
        )
        entries.append(f"{title}: {url}" if title else url)
        seen.add(url)
        if len(entries) >= limit:
            break
    return entries


def append_visible_source_urls_to_text(
    text: str,
    sources: Any,
    *,
    heading: str = "Sources",
    limit: int = 3,
) -> str:
    """Append source URLs when structured sources exist but the visible text has no URL."""

    clean_text = str(text or "").strip()
    if URL_RE.search(clean_text):
        return clean_text
    entries = source_url_entries(sources, limit=limit)
    if not entries:
        return clean_text
    source_line = f"{heading}: " + "; ".join(entries)
    return f"{clean_text}\n\n{source_line}" if clean_text else source_line


def append_visible_source_urls_to_output(
    output: Any,
    *,
    summary_field: str = "summary",
    sources_field: str = "sources",
    heading: str = "Sources",
    limit: int = 3,
) -> Any:
    """Return output with source URLs appended to its summary when possible."""

    if isinstance(output, Mapping):
        summary = str(output.get(summary_field) or "")
        updated_summary = append_visible_source_urls_to_text(
            summary,
            output.get(sources_field),
            heading=heading,
            limit=limit,
        )
        if updated_summary == summary.strip():
            return output
        updated = dict(output)
        updated[summary_field] = updated_summary
        return updated
    if not hasattr(output, summary_field):
        return output
    summary = str(getattr(output, summary_field, "") or "")
    sources = getattr(output, sources_field, None)
    updated_summary = append_visible_source_urls_to_text(
        summary,
        sources,
        heading=heading,
        limit=limit,
    )
    if updated_summary == summary.strip():
        return output
    if hasattr(output, "model_copy"):
        return output.model_copy(update={summary_field: updated_summary})
    try:
        setattr(output, summary_field, updated_summary)
    except (AttributeError, TypeError):
        return output
    return output


def _iter_sources(value: Any) -> list[Any]:
    if value is None or isinstance(value, str | bytes):
        return []
    if isinstance(value, Mapping):
        nested = value.get("sources") or value.get("source_records")
        if nested is not None:
            return _iter_sources(nested)
        return [value]
    if isinstance(value, Sequence):
        sources: list[Any] = []
        for item in value:
            sources.extend(_iter_sources(item))
        return sources
    return [value]


def _source_value(source: Any, key: str) -> str:
    if isinstance(source, Mapping):
        return str(source.get(key) or "").strip()
    return str(getattr(source, key, "") or "").strip()
