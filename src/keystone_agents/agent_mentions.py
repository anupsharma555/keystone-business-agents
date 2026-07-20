"""Natural-language agent mention parsing for CLI and Slack surfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass

from keystone_agents.schemas.orchestrator import RouteName

KNI_MENTION_RE = re.compile(r"^\s*(?:@KNI|<@[^>]+>)\s+", re.IGNORECASE)
KEYSTONE_ASK_PREFIX_RE = re.compile(r"^\s*keystone\s+ask\b\s*", re.IGNORECASE)


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
            "orch",
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
            "ba",
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
            "os",
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
            "oc",
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
            "gt",
        ),
    ),
    (
        "chief_of_staff",
        "KNI Chief of Staff Agent",
        (
            "kni chief of staff agent",
            "kni chief of staff",
            "chief of staff agent",
            "chief of staff",
            "slack operations",
            "slack ops",
            "cos",
        ),
    ),
    (
        "airtable_context_agent",
        "Airtable Context Agent",
        (
            "airtable context agent",
            "airtable context",
            "airtable agent",
            "atc",
        ),
    ),
    (
        "google_workspace_context_agent",
        "Google Workspace Context Agent",
        (
            "google workspace context agent",
            "google workspace context",
            "workspace context agent",
            "workspace context",
            "google drive context",
            "google docs context",
            "google sheets context",
            "gwc",
        ),
    ),
    (
        "zotero_context_agent",
        "Zotero Context Agent",
        (
            "zotero context agent",
            "zotero context",
            "zotero agent",
            "zc",
        ),
    ),
    (
        "rss_context_agent",
        "RSS Context Agent",
        (
            "rss context agent",
            "rss context",
            "announcements context agent",
            "announcements context",
            "rc",
        ),
    ),
    (
        "preprints_context_agent",
        "Preprints Context Agent",
        (
            "preprints context agent",
            "preprints context",
            "preprint context agent",
            "preprint context",
            "pc",
        ),
    ),
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _strip_leading_separator(value: str) -> str:
    return value.lstrip(" \t:-,;")


def _strip_bare_business_agent_prefix(value: str) -> str:
    normalized = " ".join(str(value or "").split())
    lowered = normalized.lower()
    for prefix in (
        "keystone business agents",
        "keystone business agent",
        "business agents",
        "business agent",
    ):
        if lowered == prefix:
            return ""
        if lowered.startswith(prefix + " "):
            return normalized[len(prefix) :].strip()
    return normalized


def _best_alias_match(text: str, *, context_agents_only: bool = False) -> tuple[
    RouteName, str, str
] | None:
    normalized = _normalize(text)
    best: tuple[RouteName, str, str] | None = None
    for route, agent_name, aliases in AGENT_ALIASES:
        if context_agents_only and not str(route).endswith("_context_agent"):
            continue
        for alias in aliases:
            normalized_alias = _normalize(alias)
            if normalized == normalized_alias or normalized.startswith(f"{normalized_alias} "):
                if best is None or len(normalized_alias) > len(best[2]):
                    best = (route, agent_name, normalized_alias)
    return best


def parse_agent_mention(
    text: str,
    *,
    allow_bare_context_agents: bool = False,
    allow_bare_agent_aliases: bool = False,
) -> AgentMention:
    """Parse an optional `@KNI <agent alias>` mention from user text."""

    raw = text.strip()
    match = KNI_MENTION_RE.match(raw)
    if match is None:
        if allow_bare_context_agents or allow_bare_agent_aliases:
            raw = _strip_bare_business_agent_prefix(raw)
            best = _best_alias_match(
                raw,
                context_agents_only=not allow_bare_agent_aliases,
            )
            if best is not None:
                route, agent_name, normalized_alias = best
                words_to_drop = len(normalized_alias.split())
                original_words = raw.split()
                input_text = " ".join(original_words[words_to_drop:])
                return AgentMention(
                    route=route,
                    agent_name=agent_name,
                    input_text=_strip_leading_separator(input_text),
                    explicit=True,
                )
        return AgentMention(route=None, agent_name="Keystone Orchestrator Agent", input_text=raw)

    after_mention = KEYSTONE_ASK_PREFIX_RE.sub("", raw[match.end() :].strip()).strip()
    best = _best_alias_match(after_mention)

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
