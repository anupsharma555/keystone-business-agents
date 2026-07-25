"""Shared interpretation helpers for known-contact Gmail lookups."""

from __future__ import annotations

import re

_CONTACT_WORDS = frozenset({"contact", "email", "gmail", "person", "someone"})


def looks_like_known_contact_relationship(text: str) -> bool:
    """Return whether the request describes a contact from an existing relationship."""

    lowered = " ".join(str(text or "").lower().split())
    asks_for_contact = bool(
        re.search(
            r"\b(?:email\s+address|contact\s+(?:details?|information)|"
            r"how\s+(?:can|do|should)\s+i\s+(?:reach|contact)|"
            r"what\s+(?:email|address)\b|which\s+(?:email|address)\b|"
            r"which\b[^?.\n]{0,80}\bcontact\b|"
            r"(?:find|locate|look\s+up)\b[^?.\n]{0,80}\b(?:email|address)|"
            r"who\b[^?.\n]{0,120}\b(?:set\s+up|onboarded|helped|handled|"
            r"supported|activated|registered|coordinated))",
            lowered,
        )
    )
    known_relationship = bool(
        re.search(
            r"\b(?:my|me|i)\b[^?.\n]{0,160}\b"
            r"(?:account|setup|set\s+up|onboard(?:ed|ing)?|activation|"
            r"registration|introduced|connected|spoke|emailed|messaged|"
            r"sent|invited|helped|handled|supported|coordinated)\b",
            lowered,
        )
        or re.search(
            r"\b(?:helped|set\s+up|onboarded|introduced|connected|"
            r"coordinated|worked|spoke|emailed|messaged|sent|invited|"
            r"handled|supported|registered|activated)\b"
            r"[^?.\n]{0,160}\b(?:me|my|i)\b",
            lowered,
        )
    )
    return asks_for_contact and known_relationship


def extract_known_contact_entity(text: str) -> str:
    """Extract the organization or account name that should bound the Gmail query."""

    source = str(text or "")
    patterns = (
        r"\b(?:person|contact|someone)\s+(?:from|at|with)\s+"
        r"(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)"
        r"(?=\s+(?:who|that|which|during|while|when)\b|[,?.]|$)",
        r"\bwho\s+(?:from|at|with)\s+"
        r"(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)"
        r"(?=\s+(?:helped|set\s+up|onboarded|introduced|connected|"
        r"coordinated|handled|supported|registered|activated)\b)",
        r"\bwhich\s+(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)\s+"
        r"(?:person|contact)\b",
        r"\b(?:for|from|at|with)\s+"
        r"(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)\s+"
        r"(?:person|contact)\b",
        r"\b(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)\s+"
        r"(?:person|contact)\s+(?:who|that)\b",
        r"\bmy\s+(?P<entity>[A-Za-z0-9][A-Za-z0-9&.'+ -]{1,80}?)\s+"
        r"(?:startup\s+)?(?:account|setup|onboarding|activation)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match is None:
            continue
        entity = _clean_entity(match.group("entity"))
        if entity:
            return entity
    return ""


def known_contact_gmail_query(text: str) -> str:
    """Build an exact-phrase Gmail query from a known relationship request."""

    entity = extract_known_contact_entity(text)
    return f'"{entity}"' if entity else ""


def _clean_entity(value: str) -> str:
    entity = " ".join(str(value or "").split()).strip(" ,.:;?!-'\"")
    entity = re.sub(r"^(?:the|a|an)\s+", "", entity, flags=re.I)
    entity = re.sub(
        r"\s+(?:email|gmail|startup|platform|service)$",
        "",
        entity,
        flags=re.I,
    ).strip()
    if not entity or entity.lower() in _CONTACT_WORDS:
        return ""
    return entity[:80]
