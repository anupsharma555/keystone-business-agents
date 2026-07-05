"""Pure routing text helpers for the Orchestrator agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

EMAIL_RE = re.compile(r"\bfrom:|\bto:|\bsubject:|\bmessage-id:|[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
EMAIL_WORKFLOW_RE = re.compile(
    r"\b(?:gmail|inbox|email\s+threads?|email\s+context|"
    r"my\s+emails?|recent\s+emails?|latest\s+email|unread\s+emails?|"
    r"emails?\s+from|find\s+the\s+(?:latest|most\s+recent)\s+email|"
    r"reply\s+to\s+this\s+email|draft\s+a\s+reply|prepare\s+a\s+reply|"
    r"reply\s+politely|proposed\s+recipient|subject,\s*and\s*body)\b",
    re.I,
)
URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|org|net|ai|io|health)\b", re.I)
OPPORTUNITY_RE = re.compile(
    r"\b(find|identify|search|scout|source|discover|list)\b[\s\S]*?"
    r"\b(opportunit(?:y|ies)|leads?|grants?|partners?|partnerships?|pilots?|"
    r"buyer[- ]intent|funding\s+signals?|validation\s+activity|companies?|people|individuals?|"
    r"institutes?|labs?|conferences?|rfps?|funders?|roles?|jobs?|positions?|"
    r"postings?|openings?)\b"
    r"|\b(evaluate|assess|review|qualify|recommendation|worthwhile|worth pursuing)\b[\s\S]*?"
    r"\b(opportunity|lead|recommendation|outreach|contact|conference|grant|institute|lab|person)\b",
    re.I,
)
OUTREACH_RE = re.compile(
    r"\b(?:draft|write|compose)\b[\s\S]*?\b(outreach|email|linkedin|message|note)\b"
    r"|\bprepare\b[\s\S]{0,120}\b("
    r"draft[- ]only\s+outreach|draft\s+outreach|outreach\s+(?:draft|version|email|message|note)|"
    r"email|linkedin|message|note"
    r")\b"
    r"|\boutreach draft\b|\bcold email\b"
    r"|\b(?:send\b[\s\S]*?\boutreach\s+version|strongest\s+outreach\s+version)\b",
    re.I,
)
SEND_RE = re.compile(
    r"\b(send|auto-send|autosend|deliver|publish|post|share)\b"
    r"[\s\S]{0,160}\b(email|message|outreach|linkedin|draft|version|variant|"
    r"reply|response|ceo|founder|director|recipient)\b",
    re.I,
)
NO_SEND_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never|no)\s+"
    r"(?:auto-?send|send|deliver|post|publish|share|schedule)\b"
    r"|\b(?:do\s+not|don't|dont|never)\b[\s\S]{0,160}"
    r"\b(?:auto-?send|send|deliver|post|publish|share|schedule)\b"
    r"|\bdraft[- ]only\b"
    r"|\bwithout\s+sending\b",
    re.I,
)
RESEARCH_RE = re.compile(
    r"\b(company|account|research|profile|website|url|domain|about|evaluate|"
    r"summarize|summary|synthesis|zotero|collection|article|paper|papers|"
    r"literature|conference|institute|lab|laboratory)\b",
    re.I,
)
RESUME_RE = re.compile(r"\b(resume|continue|pick up|what'?s next)\b", re.I)
NEXT_STEP_RE = re.compile(r"\bnext[- ]steps?\b", re.I)
NEXT_STEP_ARTIFACT_RE = re.compile(
    r"\b(?:recommend(?:ed|ation)?|include|list|summari[sz]e|summary|draft|"
    r"research|find|identify|write|compose|compare|evaluate|provide|format|output)"
    r"\b[\s\S]{0,120}\bnext[- ]steps?\b",
    re.I,
)
NEXT_STEP_RESUME_RE = re.compile(
    r"^\s*(?:what(?:'s| is)|show|tell me|give me|review)?\s*"
    r"(?:the\s+)?next[- ]steps?"
    r"(?:\s+(?:for|on|in)\s+(?:this|the)?\s*"
    r"(?:work\s*item|workflow|thread|request|task|run))?\s*\??\s*$"
    r"|^\s*(?:what(?:'s| is))\s+(?:the\s+)?next[- ]steps?\s+"
    r"(?:for|on|in)\s+this\b",
    re.I,
)
WORK_ITEM_NEXT_STEP_RE = re.compile(
    r"\bnext[- ]steps?\b[\s\S]{0,80}\b(?:work\s*item|workflow|thread|request|task|run|state)\b"
    r"|\b(?:work\s*item|workflow|thread|request|task|run|state)\b[\s\S]{0,80}\bnext[- ]steps?\b",
    re.I,
)


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


def looks_like_send_side_effect(text: str) -> bool:
    """Return true only for affirmative send/deliver requests."""

    cleaned = str(text or "")
    if not SEND_RE.search(cleaned):
        return False
    return not NO_SEND_RE.search(cleaned)


def looks_like_thread_local_draft_request(text: str) -> bool:
    """Return true for internal Slack-thread draft text with no provider side effect."""

    cleaned = " ".join(str(text or "").lower().split())
    if not re.search(r"\b(draft|compose|reply|respond|email|outreach)\b", cleaned):
        return False
    if not re.search(
        r"\b(thread-local|slack[- ]thread|in this thread|in slack only|"
        r"(?:post|publish|share)\s+outside\s+this\s+thread|"
        r"outside\s+this\s+thread|slack[- ]only|slack email draft|"
        r"email draft in slack)\b",
        cleaned,
    ):
        return False
    return bool(
        re.search(
            r"\b(out of scope|no external|without external|draft only|draft-only|for review)\b",
            cleaned,
        )
    )


def looks_like_resume_request(text: str) -> bool:
    """Return true for continuation asks, not requested-output next-step sections."""

    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if RESUME_RE.search(cleaned):
        return True
    if not NEXT_STEP_RE.search(cleaned):
        return False
    if NEXT_STEP_ARTIFACT_RE.search(cleaned):
        return False
    return bool(NEXT_STEP_RESUME_RE.search(cleaned) or WORK_ITEM_NEXT_STEP_RE.search(cleaned))


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
    cleaned = str(text or "")
    lower = cleaned.lower()
    if bool(EMAIL_RE.search(cleaned)) and ("subject:" in lower or "\n" in cleaned):
        return True
    if EMAIL_WORKFLOW_RE.search(cleaned):
        return not bool(
            re.search(r"\b(?:outreach|linkedin|cold\s+email|sales)\b", cleaned, flags=re.I)
            and not re.search(
                r"\b(?:gmail|inbox|email\s+threads?|current\s+email)\b", cleaned, flags=re.I
            )
        )
    return False


def looks_like_company(text: str) -> bool:
    """Return whether text looks like a researcher request."""

    cleaned = text.strip()
    if not cleaned:
        return False
    if URL_RE.search(cleaned) or RESEARCH_RE.search(cleaned):
        return True
    words = cleaned.split()
    return 1 <= len(words) <= 5 and any(word[:1].isupper() for word in words)
