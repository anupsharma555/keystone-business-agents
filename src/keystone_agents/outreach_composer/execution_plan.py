"""Local execution planning for Outreach Composer."""

from __future__ import annotations

import re

from keystone_agents.schemas.outreach_execution_plan import OutreachExecutionPlan


def _has_approved_inline_context(text: str) -> bool:
    if not text:
        return False
    if not re.search(r"\b(?:draft|write|compose|prepare)\b", text, flags=re.I):
        return False
    if not re.search(r"\b(?:outreach|email|linkedin|message|note)\b", text, flags=re.I):
        return False
    if not re.search(r"\b(?:do not send|no send|draft-only|draft only)\b", text, flags=re.I):
        return False
    return bool(
        re.search(
            r"\b(?:"
            r"approved(?:\s+(?:inline|source|source-backed|source backed))?\s+"
            r"(?:context|facts|evidence|background|grounding|rationale)"
            r"|source[-\s]+backed\s+(?:context|facts|evidence|background|grounding)"
            r"|context\s+approved\s+for\s+(?:drafting|draft-only\s+use|draft\s+only\s+use)"
            r")\s*:",
            text,
            flags=re.I,
        )
    )


def infer_outreach_execution_plan(
    request_text: str | None,
    *,
    source: str = "heuristic",
) -> OutreachExecutionPlan:
    """Infer a safe outreach drafting workflow from a direct agent request."""

    text = " ".join(str(request_text or "").split()).strip()
    lowered = text.lower()
    follow_up = "follow-up" in lowered or "follow up" in lowered or "reply" in lowered
    tracking = any(
        marker in lowered
        for marker in (
            "future replies",
            "track replies",
            "reply tracking",
            "follow-up schedule",
            "follow up schedule",
            "managed",
            "summarized",
        )
    )
    source_backed = "source-backed" in lowered or "approved" in lowered or "fixture" in lowered
    backend_test = source_backed and "opportunity" in lowered and "do not send" in lowered
    approved_inline_context = _has_approved_inline_context(text)

    return OutreachExecutionPlan(
        source=source,
        operation="draft_follow_up" if follow_up else "draft_initial_outreach",
        approved_context_required=True,
        approved_inline_context_available=approved_inline_context,
        use_default_approved_fixture_for_backend_test=backend_test,
        include_follow_up_schedule=tracking or follow_up,
        include_reply_tracking_plan=tracking,
        use_example_rag=True,
        candidate_helpers=[
            "approved_outreach_context_builder",
            "email_style_profile",
            "outreach_example_rag",
            "outreach_composer_sdk",
            "outreach_lifecycle_tracking" if tracking else "outreach_draft_only",
        ],
        rationale=(
            "Outreach must be drafted only from approved source-backed context, remain "
            "draft-only, and include reply/follow-up tracking only as internal guidance."
        ),
    )
