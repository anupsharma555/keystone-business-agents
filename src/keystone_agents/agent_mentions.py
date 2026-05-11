"""Natural-language agent mention parsing for CLI and Slack surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass

from keystone_agents.schemas.orchestrator import RouteName

KNI_MENTION_RE = re.compile(r"^\s*(?:@KNI|<@[^>]+>)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class AgentMention:
    """Resolved agent mention and remaining user request."""

    route: RouteName | None
    agent_name: str
    input_text: str
    explicit: bool = False


AGENT_ALIASES: tuple[tuple[RouteName, str, tuple[str, ...]], ...] = (
    (
        "orchestrator",
        "Keystone Orchestrator Agent",
        (
            "orchestrator agent",
            "orchestrator",
            "router",
            "routing agent",
        ),
    ),
    (
        "business_research_analyst",
        "Business Research Analyst",
        (
            "business research analyst",
            "business agent analyst",
            "business analyst",
            "research analyst",
            "analyst",
            "research agent",
            "business research",
        ),
    ),
    (
        "opportunity_scout",
        "Opportunity Scout Agent",
        (
            "opportunity scout agent",
            "opportunity scout",
            "scout agent",
            "scout",
        ),
    ),
    (
        "outreach_composer",
        "Outreach Composer Agent",
        (
            "outreach composer agent",
            "outreach composer",
            "outreach agent",
            "composer",
        ),
    ),
    (
        "gmail_triage",
        "Gmail Triage Agent",
        (
            "gmail triage agent",
            "gmail triage",
            "email triage",
            "triage agent",
            "triage",
        ),
    ),
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _strip_leading_separator(value: str) -> str:
    return value.lstrip(" \t:-,;")


def parse_agent_mention(text: str) -> AgentMention:
    """Parse an optional `@KNI <agent alias>` mention from user text."""

    raw = text.strip()
    match = KNI_MENTION_RE.match(raw)
    if match is None:
        return AgentMention(route=None, agent_name="Keystone Orchestrator Agent", input_text=raw)

    after_mention = raw[match.end() :].strip()
    normalized = _normalize(after_mention)
    best: tuple[RouteName, str, str] | None = None
    for route, agent_name, aliases in AGENT_ALIASES:
        for alias in aliases:
            normalized_alias = _normalize(alias)
            if normalized == normalized_alias or normalized.startswith(f"{normalized_alias} "):
                if best is None or len(normalized_alias) > len(best[2]):
                    best = (route, agent_name, normalized_alias)

    if best is None:
        return AgentMention(
            route="orchestrator",
            agent_name="Keystone Orchestrator Agent",
            input_text=after_mention,
            explicit=True,
        )

    route, agent_name, normalized_alias = best
    words_to_drop = len(normalized_alias.split())
    original_words = after_mention.split()
    input_text = " ".join(original_words[words_to_drop:])
    return AgentMention(
        route=route,
        agent_name=agent_name,
        input_text=_strip_leading_separator(input_text),
        explicit=True,
    )
