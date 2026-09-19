"""Shared interpretation helpers for known-contact Gmail lookups."""

from __future__ import annotations

import re

_CONTACT_WORDS = frozenset(
    {
        "associated",
        "contact",
        "email",
        "gmail",
        "person",
        "related",
        "same",
        "selected",
        "someone",
        "that",
        "this",
    }
)


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


def extract_gmail_association_entity(text: str) -> str:
    """Extract a bounded event/company anchor used to locate related Gmail.

    The provider named as context is not necessarily the provider that owns the
    action. Natural requests often use a meeting, interview, event, or account
    as a selector and then ask Gmail to find the related conversation. This
    helper extracts only the selector term; it does not choose an agent, grant
    Gmail access, or authorize a draft write.
    """

    source = " ".join(str(text or "").replace("’", "'").split())
    entity = r"[A-Za-z0-9][A-Za-z0-9&.'+ -]{0,79}?"
    business_object = r"(?:interview|meeting|event|call|appointment|account)"
    mail_object = r"(?:gmail\s+)?(?:email|message|thread|conversation)"
    temporal_selector = (
        r"(?:(?:(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday)(?:\s+(?:morning|afternoon|evening))?|"
        r"(?:this|next)\s+(?:morning|afternoon|evening|day|week))'s\s+)?"
        r"(?:(?:first|next|upcoming)\s+)?"
    )
    clock_selector = (
        r"(?:(?:at\s+)?\d{1,2}(?::\d{2})?\s*"
        r"(?:a\.?m\.?|p\.?m\.?)\s+)?"
    )
    patterns = (
        rf"\b{mail_object}\b[^.!?;]{{0,80}}"
        rf"\b(?:associated\s+with|related\s+to|connected\s+to|about|for|regarding)\s+"
        rf"(?:the\s+|my\s+|our\s+)?{temporal_selector}{clock_selector}"
        rf"(?P<entity>{entity})\s+{business_object}\b",
        rf"\b(?:use|using|from|for|about|regarding)\s+"
        rf"(?:the\s+|my\s+|our\s+)?{temporal_selector}{clock_selector}"
        rf"(?P<entity>{entity})\s+{business_object}\b"
        rf"[^.!?;]{{0,160}}\b{mail_object}\b",
        rf"\b(?P<entity>{entity})\s+{business_object}\b"
        rf"[^.!?;]{{0,160}}\b(?:associated|related|matching)?\s*{mail_object}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match is None:
            continue
        candidate = _clean_entity(match.group("entity"))
        if candidate:
            return candidate
    return ""


def associated_gmail_query(text: str) -> str:
    """Build an exact-phrase Gmail query from a bounded association anchor."""

    entity = extract_gmail_association_entity(text)
    return f'"{entity}"' if entity else ""


def _clean_entity(value: str) -> str:
    entity = " ".join(str(value or "").split()).strip(" ,.:;?!-'\"")
    entity = re.sub(r"^(?:the|a|an)\s+", "", entity, flags=re.I)
    # Association patterns may begin matching after the colon in a natural
    # time such as ``10:30 AM G2i interview``. A time fragment is selector
    # context, never part of the company/account identity used in Gmail search.
    entity = re.sub(
        r"^(?:(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\s+)+",
        "",
        entity,
        flags=re.I,
    )
    entity = re.sub(
        r"\s+(?:email|gmail|startup|platform|service)$",
        "",
        entity,
        flags=re.I,
    ).strip()
    if not entity or entity.lower() in _CONTACT_WORDS:
        return ""
    return entity[:80]
