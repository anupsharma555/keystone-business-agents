"""Shared Keystone writing style constants and prompt resource helpers."""

from __future__ import annotations

from importlib import resources

PROMPT_PACKAGE = "keystone_agents.prompts"
WRITING_STYLE_PROMPT = "writing_style.md"
KEYSTONE_DEFAULT_OUTREACH_CTA = (
    "I thought there might be mutual interest in working together around shared "
    "interests in clinical research technology. Let me know if you would like to "
    "discuss further."
)
KEYSTONE_WRITING_STYLE_GUIDANCE = (
    "Use a natural, human, concise, practical, grounded style. Keep the tone "
    "professional but not overly formal, warm but restrained, individualized, and "
    "low-pressure. Avoid hype, generic sales language, em dashes, and excessive "
    "exclamation points, and avoid sounding needy. Prioritize clarity over flourish and "
    "preserve a physician-scientist or thoughtful operator tone where appropriate. "
    "For outreach, prefer alignment, shared interests, or relevance over aggressive selling. "
    f"Default outreach CTA pattern: {KEYSTONE_DEFAULT_OUTREACH_CTA}"
)


def load_writing_style_policy() -> str:
    """Return the shared writing style prompt text."""

    return (
        resources.files(PROMPT_PACKAGE)
        .joinpath(WRITING_STYLE_PROMPT)
        .read_text(encoding="utf-8")
        .strip()
    )
