"""Text and style helpers for Gmail triage."""

from __future__ import annotations

import html
import re

from keystone_agents.schemas.email_style import EmailStyleProfile

HTML_QUOTED_BLOCK_RE = re.compile(r"<blockquote\b.*?</blockquote>", re.I | re.S)
HTML_BLOCK_TAG_RE = re.compile(r"</?(?:br|p|div|li|tr|td|h[1-6])\b[^>]*>", re.I)
HTML_TAG_RE = re.compile(r"<[^>]+>")
QUOTED_THREAD_RE = (
    re.compile(r"\s+\bOn\s.{0,160}\bwrote:\s.*\Z", re.I | re.S),
    re.compile(r"\s+-{2,}\s*Original Message\s*-{2,}.*\Z", re.I | re.S),
    re.compile(r"\s+\bFrom:\s.{0,220}\bSubject:\s.*\Z", re.I | re.S),
)


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    """Return whether any term appears in the text."""

    return any(term in text for term in terms)


def clean_text(value: str) -> str:
    """Normalize triage text without preserving quoted noise."""

    return " ".join(value.replace("\u2014", "-").split())


def normalize_body_for_triage(value: str) -> str:
    """Reduce email body noise before deterministic classification."""

    text = html.unescape(value)
    text = HTML_QUOTED_BLOCK_RE.sub(" ", text)
    text = HTML_BLOCK_TAG_RE.sub(" ", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"(?m)^\s*>.*$", " ", text)
    for pattern in QUOTED_THREAD_RE:
        text = pattern.sub(" ", text, count=1)
    return clean_text(text)


def style_greeting(sender_name: str, style_profile: EmailStyleProfile | None) -> str:
    """Return a Gmail draft greeting shaped by approved aggregate style."""

    name = sender_name.strip()
    if style_profile is None or not style_profile.greeting_patterns:
        return f"Hi {name}," if name else "Hi,"
    pattern = style_profile.greeting_patterns[0]
    if "{name}" in pattern:
        return pattern.format(name=name or "there").strip()
    if name and pattern.rstrip(",").lower() in {"hi", "hello"}:
        return f"{pattern.rstrip(',')} {name},"
    return pattern


def style_signoff(style_profile: EmailStyleProfile | None) -> str:
    """Return a Gmail draft signoff shaped by approved aggregate style."""

    if style_profile is None or not style_profile.signoffs:
        return "Sincerely,\nAnup"
    signoff = style_profile.signoffs[0].strip()
    if "\n" in signoff or any(name in signoff.lower() for name in ("keystone", "anup")):
        return signoff
    return f"{signoff}\nAnup"


def style_cta(default: str, style_profile: EmailStyleProfile | None) -> str:
    """Return a Gmail draft CTA shaped by approved aggregate style."""

    if style_profile is None:
        return default
    preferred = " ".join(style_profile.preferred_phrases).lower()
    if "compare notes" in preferred:
        return (
            "Happy to compare notes if useful. Please send any non-sensitive context and "
            "a few times that work."
        )
    if style_profile.cta_style == "calendar_offer":
        return "If useful, please send a few times that work for a brief conversation."
    if style_profile.cta_style == "context_request":
        return "Please send any non-sensitive context that would help me review fit."
    return default
