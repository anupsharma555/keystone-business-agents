"""Local execution planning for Gmail Triage."""

from __future__ import annotations

import re

from keystone_agents.schemas.gmail_execution_plan import GmailExecutionPlan


def infer_gmail_execution_plan(
    request_text: str | None,
    *,
    source: str = "heuristic",
) -> GmailExecutionPlan:
    """Infer a bounded Gmail workflow contract from a direct Gmail agent request."""

    text = " ".join(str(request_text or "").split()).strip()
    lowered = text.lower()
    lookback_days = _lookback_days(lowered) or 3
    query_terms = _query_terms(lowered)
    query = f"newer_than:{lookback_days}d"
    if query_terms:
        query = f"{query} {query_terms}"

    if _priority_grouping_request(lowered):
        priority_query = f"newer_than:{lookback_days}d"
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
            lookback_days=lookback_days,
            max_messages=_requested_count(lowered) or 5,
            gmail_query=query,
            source_label="INBOX",
            live_read_required=True,
            candidate_helpers=["gmail_thread_summary"],
            rationale="Request asks for read-only Gmail thread summarization.",
        )

    if _draft_requested(lowered):
        return GmailExecutionPlan(
            source=source,
            operation="draft_reply",
            lookback_days=lookback_days,
            max_messages=1,
            gmail_query=query,
            source_label="INBOX",
            create_gmail_drafts=False,
            draft_replies_in_output=True,
            live_read_required=True,
            candidate_helpers=["gmail_single_message_read", "gmail_triage_sdk"],
            rationale=(
                "Request asks for reply drafting; keep draft text in output unless "
                "explicitly approved."
            ),
        )

    return GmailExecutionPlan(
        source=source,
        operation="single_message_triage",
        lookback_days=lookback_days,
        max_messages=_requested_count(lowered) or 5,
        gmail_query=query,
        source_label="INBOX",
        live_read_required="gmail" in lowered or "email" in lowered,
        candidate_helpers=["gmail_message_triage"],
        rationale="Default Gmail request shape is read-only triage.",
    )


def _priority_grouping_request(lowered: str) -> bool:
    return any(marker in lowered for marker in ("top ", "top 3", "priority", "actionable")) or (
        "recent" in lowered and ("threads" in lowered or "messages" in lowered)
    )


def _draft_requested(lowered: str) -> bool:
    return any(marker in lowered for marker in ("draft", "reply", "respond"))


def _lookback_days(lowered: str) -> int | None:
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


def _query_terms(lowered: str) -> str:
    terms: list[str] = []
    if "keystone" in lowered:
        terms.append("Keystone")
    if "opportunit" in lowered:
        terms.append("opportunity")
    if "follow-up" in lowered or "follow up" in lowered or "followup" in lowered:
        terms.append('"follow up"')
    return " ".join(dict.fromkeys(terms))
