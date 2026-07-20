"""Shared transient policy for requests that imply current or recent evidence."""

from __future__ import annotations

import re
from typing import Any

_TEMPORAL_TERM_RE = re.compile(
    r"\b(?:current|currently|latest|newest|recent|recently|today|this\s+(?:week|month|year)|"
    r"now|fresh|up\s+to\s+date|up-to-date|202[5-9]|203\d)\b",
    re.I,
)


def temporal_depth_policy(
    request_text: str,
    *,
    tool_budget_exhausted: bool = False,
    provider_system: str = "unspecified",
    requires_live_search: bool | None = None,
) -> dict[str, Any]:
    """Return a prompt-safe transient evidence-depth policy for one request."""

    triggers = _temporal_triggers(request_text)
    provider_bound = str(provider_system or "unspecified") != "unspecified"
    has_temporal_intent = bool(triggers) and (
        bool(requires_live_search)
        if requires_live_search is not None
        else not provider_bound
    )
    if not has_temporal_intent:
        triggers = []
    policy = {
        "schema": "keystone.temporal_depth_policy.v1",
        "temporal_intent": has_temporal_intent,
        "trigger_terms": triggers,
        "source_recency_requirement": "recent_or_current" if has_temporal_intent else "as_requested",
        "independent_validation": (
            "required_when_available" if has_temporal_intent else "standard_source_sufficiency"
        ),
        "source_read_through": (
            "read_selected_sources_before_synthesis"
            if has_temporal_intent
            else "read_sources_when_claims_depend_on_them"
        ),
        "tool_budget_exhausted": bool(tool_budget_exhausted),
        "completion_rule": (
            "If recent/current independent evidence cannot be found within the available "
            "tool budget, say not enough evidence yet and name the missing evidence instead "
            "of presenting stale or weak evidence as complete."
            if has_temporal_intent
            else "Use normal source sufficiency and blocker rules."
        ),
    }
    if has_temporal_intent:
        policy["required_final_answer_behavior"] = (
            "Separate current facts from older context, include source URLs for external "
            "claims, and state source freshness limits visibly."
        )
    return policy


def _temporal_triggers(request_text: str) -> list[str]:
    text = str(request_text or "")
    seen: set[str] = set()
    triggers: list[str] = []
    for match in _TEMPORAL_TERM_RE.finditer(text):
        trigger = " ".join(match.group(0).lower().split())
        if trigger not in seen:
            seen.add(trigger)
            triggers.append(trigger)
    return triggers[:8]
