"""Text and style helpers for Outreach Composer."""

from __future__ import annotations

from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.writing_style import KEYSTONE_DEFAULT_OUTREACH_CTA


def clean_copy(value: str | None) -> str:
    """Normalize outbound copy text without changing meaning."""

    return (value or "").replace("\u2014", "-").strip()


def salutation_name(contact_name: str | None) -> str:
    """Return the contact name segment suitable for a greeting."""

    cleaned = clean_copy(contact_name)
    if not cleaned:
        return ""
    if cleaned.lower().startswith("dr. "):
        return cleaned
    return cleaned.split()[0]


def sentence_fragment(value: str) -> str:
    """Return a lower-case sentence fragment for embedded copy."""

    return clean_copy(value).rstrip(".!?").lower()


def shorten_text(value: str, max_chars: int) -> str:
    """Shorten text at a word boundary when possible."""

    cleaned = clean_copy(value)
    if len(cleaned) <= max_chars:
        return cleaned
    shortened = cleaned[:max_chars].rsplit(" ", 1)[0].rstrip(" .,;:")
    return shortened or cleaned[:max_chars].rstrip(" .,;:")


def bounded_outreach_goal(
    value: str | None,
    *,
    max_words: int = 32,
    max_chars: int = 240,
) -> str:
    """Keep workflow objectives from becoming unbounded outbound copy."""

    cleaned = " ".join(clean_copy(value).split())
    if not cleaned:
        return ""
    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words]).rstrip(" .,;:")
    return shorten_text(cleaned, max_chars)


def normalized_text(value: str) -> str:
    """Normalize text for approximate matching."""

    return " ".join(clean_copy(value).lower().split())


def token_set(value: str) -> set[str]:
    """Return normalized tokens longer than two characters."""

    return {
        token.strip(".,;:!?()[]{}\"'").lower()
        for token in value.split()
        if len(token.strip(".,;:!?()[]{}\"'")) > 2
    }


def looks_like_drafting_instruction(value: str) -> bool:
    """Return whether text looks like an instruction rather than a CTA topic."""

    return value.lower().startswith(("write ", "draft ", "create ", "compose "))


def cta_topic_from_goal(goal: str) -> str:
    """Convert an outreach goal into a safe CTA topic."""

    cleaned = clean_copy(goal) or "compare notes on clinical AI evaluation support"
    lowered = cleaned.lower()
    if looks_like_drafting_instruction(cleaned):
        if "keystone" in lowered and "fit" in lowered:
            return "compare notes on Keystone's fit"
        if "clinical" in lowered or "research" in lowered:
            return "compare notes on clinical research operations"
        return "compare notes"
    return cleaned


def style_greeting(
    salutation: str,
    style_profile: EmailStyleProfile | None,
) -> str:
    """Return a greeting shaped by an approved aggregate style profile."""

    if style_profile is None or not style_profile.greeting_patterns:
        return f"Hi {salutation}," if salutation else "Hello,"
    pattern = style_profile.greeting_patterns[0]
    if "{name}" in pattern:
        return pattern.format(name=salutation or "there").strip()
    if salutation and pattern.rstrip(",").lower() in {"hi", "hello"}:
        return f"{pattern.rstrip(',')} {salutation},"
    return pattern


def style_signoff(style_profile: EmailStyleProfile | None) -> str:
    """Return a safe signoff shaped by an approved aggregate style profile."""

    if style_profile is None or not style_profile.signoffs:
        return "Sincerely,\nAnup"
    signoff = style_profile.signoffs[0].strip()
    if "\n" in signoff or any(name in signoff.lower() for name in ("keystone", "anup")):
        return signoff
    return f"{signoff}\nAnup"


def style_cta(
    goal: str,
    style_profile: EmailStyleProfile | None,
) -> str:
    """Return a safe CTA shaped by an approved aggregate style profile."""

    raw_goal = clean_copy(goal)
    goal = cta_topic_from_goal(goal)
    if looks_like_drafting_instruction(raw_goal):
        return KEYSTONE_DEFAULT_OUTREACH_CTA
    default = (
        f"I am reaching out to see whether it would be useful to {goal}. "
        "If relevant, I would welcome a brief introductory conversation."
    )
    if style_profile is None:
        return default
    preferred = " ".join(style_profile.preferred_phrases).lower()
    if "compare notes" in preferred:
        if "compare notes" in goal.lower():
            return "Happy to compare notes if useful."
        return f"Happy to compare notes on {goal} if useful."
    if style_profile.cta_style == "calendar_offer":
        return f"Open to a short conversation about {goal} next week?"
    if style_profile.cta_style == "context_request":
        return f"Could you send non-sensitive context on whether {goal} would be useful?"
    return default
