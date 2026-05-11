"""Pure routing text helpers for the Orchestrator agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EMAIL_RE = re.compile(r"\bfrom:|\bto:|\bsubject:|\bmessage-id:|[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|ai|io|health)\b", re.I)
OPPORTUNITY_RE = re.compile(
    r"\b(find|identify|search|scout|source|discover|list)\b.*"
    r"\b(opportunities?|leads?|grants?|partners?|companies?|people|individuals?|"
    r"institutes?|labs?|conferences?|rfps?|funders?)\b"
    r"|\b(evaluate|assess|review|qualify|recommendation|worthwhile|worth pursuing)\b.*"
    r"\b(opportunity|lead|recommendation|outreach|contact|conference|grant|institute|lab|person)\b",
    re.I,
)
OUTREACH_RE = re.compile(
    r"\b(draft|write|compose|prepare)\b.*\b(outreach|email|linkedin|message|note)\b"
    r"|\boutreach draft\b|\bcold email\b",
    re.I,
)
SEND_RE = re.compile(r"\b(send|auto-send|autosend|deliver)\b.*\b(email|message|outreach)\b", re.I)
RESEARCH_RE = re.compile(
    r"\b(company|account|research|profile|website|url|domain|about|evaluate|"
    r"summarize|summary|synthesis|zotero|collection|article|paper|papers|"
    r"literature|conference|institute|lab|laboratory)\b",
    re.I,
)
RESUME_RE = re.compile(r"\b(resume|continue|pick up|next step|what'?s next)\b", re.I)


def payload_text(payload: str | Mapping[str, Any] | None) -> str:
    """Extract routeable text from a string or structured payload."""

    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()

    parts: list[str] = []
    for key in (
        "input",
        "text",
        "query",
        "request",
        "message",
        "subject",
        "from",
        "to",
        "body",
        "url",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}: {value.strip()}")
    return "\n".join(parts).strip()


def mapping_value(payload: str | Mapping[str, Any] | None, *keys: str) -> Any:
    """Return the first matching mapping value from a structured payload."""

    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def looks_like_email(payload: str | Mapping[str, Any] | None, text: str) -> bool:
    """Return whether payload/text looks like an email workflow request."""

    if isinstance(payload, Mapping):
        email_keys = {
            "subject",
            "from",
            "to",
            "sender",
            "sender_email",
            "body",
            "message_id",
            "thread_id",
        }
        if len(email_keys & set(payload)) >= 2:
            return True
    return bool(EMAIL_RE.search(text)) and ("subject:" in text.lower() or "\n" in text)


def looks_like_company(text: str) -> bool:
    """Return whether text looks like a researcher request."""

    cleaned = text.strip()
    if not cleaned:
        return False
    if URL_RE.search(cleaned) or RESEARCH_RE.search(cleaned):
        return True
    words = cleaned.split()
    return 1 <= len(words) <= 5 and any(word[:1].isupper() for word in words)
