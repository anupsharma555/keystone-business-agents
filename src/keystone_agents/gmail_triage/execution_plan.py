"""Local execution planning for Gmail Triage."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from keystone_agents.schemas.gmail_execution_plan import GmailExecutionPlan


def infer_gmail_execution_plan(
    request_text: str | None,
    *,
    source: str = "heuristic",
) -> GmailExecutionPlan:
    """Infer a bounded Gmail workflow contract from a direct Gmail agent request."""

    text = " ".join(str(request_text or "").split()).strip()
    lowered = text.lower()
    read_scope = "message" if _single_message_request(lowered) else "thread"
    explicit_lookback_days = _lookback_days(lowered)
    lookback_days = explicit_lookback_days or 3
    subject_hint = extract_gmail_subject_hint(text)
    query_terms = _query_terms(text)
    query = (
        _date_scope_query(lowered, lookback_days)
        if explicit_lookback_days is not None or not subject_hint
        else ""
    )
    if query_terms:
        query = " ".join(part for part in (query, query_terms) if part)

    if _inline_context_request(lowered):
        return GmailExecutionPlan(
            source=source,
            operation="single_message_triage",
            read_scope=read_scope,
            lookback_days=lookback_days,
            max_messages=1,
            gmail_query="",
            source_label="inline_context",
            create_gmail_drafts=False,
            draft_replies_in_output=False,
            live_read_required=False,
            candidate_helpers=["inline_email_context_triage", "gmail_triage_sdk"],
            rationale=(
                "Request provides inline email context and asks to use only that context; "
                "do not read live Gmail."
            ),
        )

    if _style_profile_request(lowered):
        return GmailExecutionPlan(
            source=source,
            operation="style_profile",
            lookback_days=lookback_days,
            max_messages=min(_requested_count(lowered) or 5, 25),
            gmail_query="",
            source_label="SENT",
            create_gmail_drafts=False,
            draft_replies_in_output=_draft_requested(lowered),
            live_read_required=True,
            candidate_helpers=[
                "gmail_sent_message_sample",
                "aggregate_email_style_profile_build",
                "email_style_profile_human_approval",
                "gmail_triage_sdk",
            ],
            rationale=(
                "Request asks Gmail Triage to learn bounded aggregate drafting style from "
                "sent mail, then use only an approved redacted profile for draft guidance."
            ),
            planner_warnings=[
                "Raw sent bodies stay in memory only and must not be stored in profiles, "
                "traces, fixtures, or reports.",
                "Generated profiles require human approval before drafting use.",
            ],
        )

    if _priority_grouping_request(lowered):
        priority_query = _date_scope_query(lowered, lookback_days)
        return GmailExecutionPlan(
            source=source,
            operation="priority_grouping",
            lookback_days=lookback_days,
            max_messages=_requested_count(lowered) or 10,
            gmail_query=priority_query,
            source_label="INBOX",
            create_gmail_drafts=False,
            draft_replies_in_output=_draft_requested(lowered),
            live_read_required=True,
            candidate_helpers=[
                "gmail_search_summaries",
                "gmail_batch_message_read",
                "gmail_thread_expansion_when_needed",
                "gmail_priority_grouping_sdk",
            ],
            rationale=(
                "Request asks for recent Gmail threads/messages to be ranked and summarized; "
                "use broad bounded retrieval before relevance filtering so Gmail search "
                "AND semantics do not hide relevant threads."
            ),
        )

    if "thread" in lowered and ("summarize" in lowered or "summary" in lowered):
        return GmailExecutionPlan(
            source=source,
            operation="thread_summary",
            read_scope="thread",
            lookback_days=lookback_days,
            max_messages=_requested_count(lowered) or 5,
            gmail_query=query,
            source_label="INBOX",
            live_read_required=True,
            candidate_helpers=["gmail_thread_summary"],
            rationale="Request asks for read-only Gmail thread summarization.",
        )

    if _draft_update_request(lowered):
        draft_subject_hint = _draft_subject_hint(text)
        draft_recipient_hint = _draft_recipient_hint(text)
        return GmailExecutionPlan(
            source=source,
            operation="update_draft",
            lookback_days=lookback_days,
            max_messages=1,
            gmail_query="",
            source_label="DRAFT",
            draft_subject_hint=draft_subject_hint,
            draft_recipient_hint=draft_recipient_hint,
            create_gmail_drafts=True,
            draft_replies_in_output=True,
            live_read_required=True,
            candidate_helpers=["gmail_draft_read", "gmail_draft_update"],
            artifact_policy="update_verified_gmail_draft",
            side_effect_policy="scoped_gmail_draft_write_no_send",
            rationale=(
                "Request asks to revise an existing Gmail draft; resolve the exact draft, "
                "read it before editing, update the same draft, and verify the result."
            ),
            planner_warnings=(
                []
                if draft_subject_hint or draft_recipient_hint
                else [
                    "An unambiguous selected draft or natural subject/recipient reference "
                    "is required before update."
                ]
            ),
        )

    if _draft_requested(lowered):
        create_provider_draft = _provider_draft_write_requested(lowered)
        return GmailExecutionPlan(
            source=source,
            operation="draft_reply",
            read_scope=read_scope,
            lookback_days=lookback_days,
            max_messages=1,
            gmail_query=query,
            source_label="INBOX",
            create_gmail_drafts=create_provider_draft,
            draft_replies_in_output=True,
            live_read_required=True,
            candidate_helpers=[
                "gmail_single_message_read",
                "gmail_triage_sdk",
                *(
                    ["gmail_verified_reply_draft_create"]
                    if create_provider_draft
                    else []
                ),
            ],
            artifact_policy=(
                "create_verified_gmail_draft"
                if create_provider_draft
                else "draft_text_in_output"
            ),
            side_effect_policy=(
                "scoped_gmail_draft_write_no_send"
                if create_provider_draft
                else "read_only_or_draft_only"
            ),
            rationale=(
                "Request asks for reply drafting; keep draft text in output unless "
                "explicitly approved."
            ),
        )

    return GmailExecutionPlan(
        source=source,
        operation="single_message_triage",
        read_scope=read_scope,
        lookback_days=lookback_days,
        max_messages=_requested_count(lowered) or 5,
        gmail_query=query,
        source_label="INBOX",
        live_read_required="gmail" in lowered or "email" in lowered,
        candidate_helpers=["gmail_message_triage"],
        rationale="Default Gmail request shape is read-only triage.",
    )


def _whole_thread_request(lowered: str) -> bool:
    """Return true only when the operator names a Gmail thread/conversation."""

    return bool(re.search(r"\b(?:gmail\s+)?(?:thread|conversation)\b", lowered))


def _single_message_request(lowered: str) -> bool:
    if _whole_thread_request(lowered):
        return False
    return bool(
        re.search(
            r"\b(?:latest|newest|most\s+recent)\s+(?:gmail\s+)?(?:email|message)\b",
            lowered,
        )
    )


def _priority_grouping_request(lowered: str) -> bool:
    if any(marker in lowered for marker in ("top ", "top 3", "priority", "actionable")):
        return True
    if "recent" in lowered and ("threads" in lowered or "messages" in lowered):
        return True
    return bool(
        re.search(r"\b(?:summarize|summary|review|recap)\b", lowered)
        and re.search(r"\b(?:emails|messages|inbox)\b", lowered)
        and re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", lowered)
    )


def _style_profile_request(lowered: str) -> bool:
    has_mail_context = bool(
        re.search(r"\b(?:gmail|emails?|sent\s+(?:mail|emails?|messages?))\b", lowered)
    )
    explicit_style_learning = bool(
        re.search(
            r"\b(?:learn|infer|derive|review|analy[sz]e)\b.{0,50}"
            r"\b(?:style|tone|voice|wording|phrasing)\b|"
            r"\b(?:write\s+like|similar\s+style|mimic|match\s+my\s+(?:style|tone|voice))\b",
            lowered,
        )
    )
    sent_style_context = bool(
        re.search(r"\bsent\s+(?:mail|emails?|messages?)\b", lowered)
        and re.search(r"\b(?:style|tone|voice|wording|phrasing)\b", lowered)
    )
    return has_mail_context and (explicit_style_learning or sent_style_context)


def _inline_context_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "use only this inline",
            "inline email context",
            "inline, non-sensitive email context",
            "sanitized inline email",
            "provided email context",
            "provided inline context",
            "do not access gmail",
            "do not access live gmail",
        )
    )


def _draft_requested(lowered: str) -> bool:
    return any(marker in lowered for marker in ("draft", "reply", "respond"))


def _provider_draft_write_requested(lowered: str) -> bool:
    if re.search(
        r"\b(?:do not|don't|dont)\b[^.!?]{0,120}"
        r"\b(?:create|save|write|add)\b[^.!?]{0,40}\b(?:gmail\s+)?draft\b",
        lowered,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:create|save|write|add)\b.{0,30}\b(?:gmail\s+)?draft\b",
            lowered,
        )
        or re.search(r"\b(?:save|put)\b.{0,25}\b(?:in|to)\s+gmail\b", lowered)
    )


def _draft_update_request(lowered: str) -> bool:
    return bool(
        re.search(r"\b(?:existing|current|selected|this)\s+(?:gmail\s+)?draft\b", lowered)
        and re.search(
            r"\b(?:update|edit|revise|rewrite|shorter|warmer|longer|change|modify)\b",
            lowered,
        )
    )


def _draft_subject_hint(text: str) -> str:
    for pattern in (
        r"\b(?:subject|titled|called)\s+[\"'](?P<value>[^\"']{2,160})[\"']",
        r"\bsubject\s+(?P<value>[^,.;]{2,160})(?=\s+(?:for|to)\b|[,.;]|$)",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group("value").split()).strip()
    return ""


def _draft_recipient_hint(text: str) -> str:
    match = re.search(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", text, flags=re.IGNORECASE)
    return match.group(0).strip() if match else ""


def _lookback_days(lowered: str) -> int | None:
    if re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", lowered):
        return 1
    match = re.search(r"\b(?:last|past|recent)\s+(\d{1,3})\s+days?\b", lowered)
    if not match:
        return None
    try:
        return max(1, min(365, int(match.group(1))))
    except ValueError:
        return None


def _requested_count(lowered: str) -> int | None:
    match = re.search(r"\btop\s+(\d{1,2})\b", lowered)
    if not match:
        return None
    try:
        return max(1, min(50, int(match.group(1)) * 4))
    except ValueError:
        return None


def extract_gmail_subject_hint(text: str) -> str:
    """Extract a bounded Gmail subject named by ordinary operator wording."""

    normalized = " ".join(str(text or "").split()).strip()
    subject_label = (
        r"(?:email\s+)?(?:with\s+(?:the\s+)?subject(?:\s+line)?|"
        r"subject(?:\s+line)?|titled|called)"
    )
    patterns = (
        rf"\b{subject_label}\s*:?\s*[\"“‘']"
        r"(?P<value>[^\"”’']{2,160})[\"”’']",
        rf"\b{subject_label}\s*:?\s*"
        r"(?P<value>[A-Za-z0-9][^.!?\n]{1,159}?)"
        r"(?=(?:[.!?](?:\s|$)|$|\s+(?:and\s+)?"
        r"(?:read|review|summari[sz]e|tell|then|draft|reply|respond|"
        r"do\s+not|don't|dont|without)\b))",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        value = " ".join(match.group("value").split()).strip(" \t,:;\"'“”‘’")
        if 2 <= len(value) <= 160:
            return value
    return ""


def _gmail_subject_query_term(text: str) -> str:
    subject = extract_gmail_subject_hint(text)
    if not subject:
        return ""
    safe_subject = subject.replace("\\", " ").replace('"', "'")
    return f'subject:"{safe_subject}"'


def _query_terms(text: str) -> str:
    lowered = text.lower()
    terms: list[str] = []
    subject_query = _gmail_subject_query_term(text)
    if subject_query:
        terms.append(subject_query)
    if "keystone" in lowered:
        terms.append("Keystone")
    if "opportunit" in lowered:
        terms.append("opportunity")
    if "follow-up" in lowered or "follow up" in lowered or "followup" in lowered:
        terms.append('"follow up"')
    sender_hint = _email_sender_hint(lowered)
    if sender_hint:
        terms.append(f'"{sender_hint}"')
    return " ".join(dict.fromkeys(terms))


def _date_scope_query(lowered: str, lookback_days: int) -> str:
    if re.search(r"\b(?:today|this\s+morning|this\s+afternoon)\b", lowered):
        today = datetime.now(ZoneInfo("America/New_York")).date()
        return f"after:{today.strftime('%Y/%m/%d')}"
    return f"newer_than:{lookback_days}d"


def _email_sender_hint(lowered: str) -> str:
    match = re.search(
        r"\b(?:email|message|thread)s?\s+from\s+"
        r"(?P<sender>[a-z0-9][a-z0-9&.' -]{1,80}?)"
        r"(?=\s+(?:and|that|about|with|then|to)\b|[,.?]|$)",
        lowered,
    )
    if not match:
        return ""
    sender = " ".join(match.group("sender").split()).strip()
    if re.fullmatch(r"(?:today|yesterday|the\s+(?:last|past)\s+\d+\s+days?)", sender):
        return ""
    return sender
