"""Shared helpers for opt-in run cost tracking directives."""

from __future__ import annotations

import re
from dataclasses import dataclass

_COST_TRACKING_DIRECTIVE_RE = re.compile(
    r"(?:^|\b)(?:also\s+)?(?:keep|track|include|show|record|report)\s+"
    r"(?:track\s+of\s+)?(?:this\s+|the\s+|current\s+)?run\s+costs?\b"
    r"|(?:^|\b)(?:this\s+run\s+|run\s+)?cost\s+tracking\b"
    r"|(?:^|\b)track\s+(?:this\s+|the\s+|current\s+)?run\s+costs?\b",
    flags=re.I,
)


@dataclass(frozen=True)
class CostTrackingDirective:
    """Parsed cost-tracking request text."""

    requested: bool
    cleaned_text: str


def parse_cost_tracking_directive(text: str | None) -> CostTrackingDirective:
    """Return whether the operator asked for cost tracking and a cleaned request."""

    raw = str(text or "")
    requested = bool(_COST_TRACKING_DIRECTIVE_RE.search(raw))
    if not requested:
        return CostTrackingDirective(requested=False, cleaned_text=raw)
    cleaned = _COST_TRACKING_DIRECTIVE_RE.sub("", raw)
    cleaned = re.sub(r"\s+(?:and|also)\s*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"[\s,;:.]+$", "", cleaned).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return CostTrackingDirective(requested=True, cleaned_text=cleaned)


def cost_tracking_requested(text: str | None) -> bool:
    """Return true when request text includes a cost-tracking directive."""

    return parse_cost_tracking_directive(text).requested


def append_cost_tracking_directive(text: str) -> str:
    """Append the standard operator phrase when missing."""

    if cost_tracking_requested(text):
        return text
    separator = " " if str(text or "").strip() else ""
    return f"{str(text or '').strip()}{separator}Also keep track of this run costs."
