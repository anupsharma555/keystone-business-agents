"""Sanitize successful outreach threads into local retrieval examples."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from keystone_agents.schemas.outreach_examples import (
    OutreachExampleDocument,
    OutreachExampleMessageSummary,
    OutreachExampleOutcome,
    OutreachExampleRecord,
    OutreachExampleRetrievalResult,
    OutreachExampleStage,
    OutreachExampleThread,
    RetrievedOutreachExample,
    hash_thread_id,
)

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>'\"`]+", re.IGNORECASE)
SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^'\"\s,;]+)",
    re.IGNORECASE,
)
MRN_RE = re.compile(
    r"\b(?:mrn|medical record number|dob|date of birth|ssn|social security)\b"
    r"\s*[:=]?\s*[^,;.\n]*",
    re.IGNORECASE,
)
PATIENT_RE = re.compile(
    r"\bpatient\s+[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?\b[^.\n]*"
    r"\b(?:diagnos(?:is|ed)|treatment|medication|therapy|depression|anxiety|"
    r"bipolar|schizophrenia|psychiatric|trial)\b[^.\n]*",
    re.IGNORECASE,
)
LONG_NUMBER_RE = re.compile(r"\b\d{6,}\b")
RAW_HEADER_RE = re.compile(
    r"\b(?:from|to|cc|bcc|subject|date|message-id|reply-to|received):\s*\S+",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")
PRIVATE_RETRIEVAL_MARKERS = (
    "[REDACTED_EMAIL]",
    "[REDACTED_PHONE]",
    "[REDACTED_SECRET]",
    "[REDACTED_PHI]",
    "private raw",
    "raw body",
)
RETRIEVAL_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "company",
    "clinical",
    "outreach",
    "support",
}
KEY_PHRASE_CANDIDATES = (
    "compare notes",
    "brief introductory conversation",
    "if useful",
    "clinical AI evaluation",
    "research operations",
    "happy to",
)


def _clean(value: Any) -> str:
    return WHITESPACE_RE.sub(" ", str(value or "").replace("\u2014", "-")).strip()


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def sanitize_outreach_example_text(text: str) -> tuple[str, list[str]]:
    """Return sanitized text and sensitivity flags for local example storage."""

    sanitized = str(text or "").replace("\u2014", "-")
    flags: list[str] = []
    replacements: tuple[tuple[re.Pattern[str], str, str], ...] = (
        (SECRET_RE, "[REDACTED_SECRET]", "secret"),
        (EMAIL_RE, "[REDACTED_EMAIL]", "email"),
        (PHONE_RE, "[REDACTED_PHONE]", "phone"),
        (URL_RE, "[REDACTED_URL]", "url"),
        (MRN_RE, "[REDACTED_PHI]", "possible_phi"),
        (PATIENT_RE, "[REDACTED_PHI]", "possible_phi"),
        (LONG_NUMBER_RE, "[REDACTED_NUMBER]", "identifier"),
    )
    for pattern, replacement, flag in replacements:
        sanitized, count = pattern.subn(replacement, sanitized)
        if count:
            flags.append(flag)
    return _clean(sanitized), list(dict.fromkeys(flags))


def _message_body(message: Mapping[str, Any]) -> str:
    return str(
        message.get("normalized_body")
        or message.get("body")
        or message.get("text")
        or message.get("snippet")
        or ""
    )


def _message_subject(message: Mapping[str, Any]) -> str:
    return str(message.get("subject") or "")


def _message_id(message: Mapping[str, Any], index: int) -> str:
    return str(message.get("id") or message.get("message_id") or f"message-{index}")


def _message_direction(message: Mapping[str, Any]) -> str:
    labels = {str(label).upper() for label in message.get("labelIds", []) or []}
    sender = str(message.get("from") or message.get("sender_email") or "").lower()
    if "SENT" in labels:
        return "outbound"
    if "DRAFT" in labels:
        return "outbound"
    if sender and ("keystone" in sender or "anup" in sender):
        return "outbound"
    return "inbound"


def _message_summary(message: Mapping[str, Any], index: int) -> OutreachExampleMessageSummary:
    body = _message_body(message)
    subject = _message_subject(message)
    sanitized_body, body_flags = sanitize_outreach_example_text(body)
    sanitized_subject, subject_flags = sanitize_outreach_example_text(subject)
    intent = _infer_intent(sanitized_body)
    summary = _summarized_message_text(sanitized_body, intent, body_flags)
    if not summary and sanitized_subject:
        summary = "Subject-only context was available; no body content was retained."
    if not summary:
        summary = "No usable sanitized content."
    return OutreachExampleMessageSummary(
        message_id_hash=_hash(_message_id(message, index)),
        direction=_message_direction(message),
        sent_at=str(message.get("received_at") or message.get("date") or ""),
        sender_role="keystone" if _message_direction(message) == "outbound" else "recipient",
        subject_summary=sanitized_subject[:160],
        sanitized_summary=summary,
        intent=intent,
        sensitive_flags=list(dict.fromkeys([*body_flags, *subject_flags])),
        raw_body_included=False,
    )


def _summarized_message_text(text: str, intent: str, flags: Sequence[str]) -> str:
    """Return derived message traits without storing reusable body excerpts."""

    if not text:
        return ""
    lowered = text.lower()
    key_phrases = [phrase for phrase in KEY_PHRASE_CANDIDATES if phrase in lowered][:4]
    topic_terms = [
        label
        for term, label in (
            ("clinical", "clinical context"),
            ("evaluation", "evaluation context"),
            ("research", "research context"),
            ("operations", "operations context"),
            ("workflow", "workflow context"),
            ("evidence", "evidence context"),
        )
        if term in lowered
    ]
    word_count = len(re.findall(r"[A-Za-z0-9]+", text))
    parts = [
        f"Derived message summary: {intent.replace('_', ' ')}",
        f"word count {word_count}",
    ]
    if topic_terms:
        parts.append("topics " + ", ".join(list(dict.fromkeys(topic_terms))[:4]))
    if key_phrases:
        parts.append("phrases " + ", ".join(key_phrases))
    if flags:
        parts.append("sensitive content was redacted before summarization")
    return "; ".join(parts) + "."


def _infer_intent(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in ("thanks", "thank you", "appreciate")):
        return "relationship_acknowledgment"
    if any(term in lowered for term in ("meet", "call", "conversation", "compare notes")):
        return "meeting_or_discussion"
    if any(term in lowered for term in ("follow up", "checking in")):
        return "follow_up"
    if any(term in lowered for term in ("interested", "useful", "helpful")):
        return "positive_reply"
    return "outreach_context"


def _conversation_pattern(messages: Sequence[OutreachExampleMessageSummary]) -> str:
    directions = [message.direction for message in messages]
    if directions[:3] == ["outbound", "inbound", "outbound"]:
        return "Concise initial outreach, recipient reply, then short contextual follow-up."
    if "inbound" in directions and "outbound" in directions:
        return "Thread balances a short Keystone message with a recipient response."
    if directions and all(direction == "outbound" for direction in directions):
        return "Outbound-only thread with concise context and a low-pressure CTA."
    return "Sanitized successful outreach pattern with limited thread structure."


def _effective_phrases(text: str) -> list[str]:
    lowered = text.lower()
    return [phrase for phrase in KEY_PHRASE_CANDIDATES if phrase in lowered][:5]


def _cta_pattern(text: str) -> str:
    lowered = text.lower()
    if "compare notes" in lowered:
        return "Soft CTA to compare notes."
    if "brief" in lowered and ("call" in lowered or "conversation" in lowered):
        return "Brief introductory conversation request."
    if "open to" in lowered:
        return "Low-pressure open-ended invitation."
    return "No direct CTA stored; use a low-pressure question."


def _follow_up_pattern(messages: Sequence[OutreachExampleMessageSummary]) -> str:
    if len(messages) >= 3:
        return "Follow up only after a recipient signal, keeping the reply concise."
    return "No multi-step follow-up pattern captured."


def _reply_pattern(messages: Sequence[OutreachExampleMessageSummary]) -> str:
    if any(message.direction == "inbound" for message in messages):
        return "Recipient reply was summarized and used only as sanitized context."
    return "No recipient reply pattern captured."


def _lessons(messages: Sequence[OutreachExampleMessageSummary]) -> list[str]:
    lessons = ["Use concise, source-backed context and avoid unsupported outcome claims."]
    if any(message.direction == "inbound" for message in messages):
        lessons.append("Keep replies responsive to the recipient's stated interest.")
    if any("possible_phi" in message.sensitive_flags for message in messages):
        lessons.append("Exclude patient-specific details from reusable examples.")
    return lessons


def outreach_example_document_from_gmail_thread(
    thread: Mapping[str, Any],
    *,
    source_label: str,
    channel: str = "email",
    outreach_stage: OutreachExampleStage = "initial_outreach",
    outcome: OutreachExampleOutcome = "success",
    company_type: str = "",
    opportunity_type: str = "",
    template_id: str | None = None,
    style_profile_id: str | None = None,
    approved_for_drafting: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> OutreachExampleDocument:
    """Convert one Gmail thread response into a sanitized local RAG document."""

    raw_messages = thread.get("messages") if isinstance(thread.get("messages"), list) else []
    messages = [
        _message_summary(message, index)
        for index, message in enumerate(raw_messages)
        if isinstance(message, Mapping)
    ]
    combined = " ".join(message.sanitized_summary for message in messages)
    thread_summary = combined[:700] or "Successful outreach thread captured without raw body."
    sensitive_flags = list(
        dict.fromkeys(flag for message in messages for flag in message.sensitive_flags)
    )
    thread_record = OutreachExampleThread(
        source_thread_id_hash=hash_thread_id(str(thread.get("id") or thread.get("threadId") or "")),
        source_label=source_label,
        channel=channel,  # type: ignore[arg-type]
        outcome=outcome,
        messages=messages,
        sanitized_thread_summary=thread_summary,
        sensitive_flags=sensitive_flags,
        raw_body_included=False,
    )
    text = " ".join(
        [
            thread_record.sanitized_thread_summary,
            " ".join(message.subject_summary for message in messages),
            " ".join(message.intent for message in messages),
        ]
    )
    return OutreachExampleDocument(
        source_thread_id_hash=thread_record.source_thread_id_hash,
        source_label=thread_record.source_label,
        channel=thread_record.channel,
        outreach_stage=outreach_stage,
        outcome=thread_record.outcome,
        company_type=company_type,
        opportunity_type=opportunity_type,
        template_id=template_id,
        style_profile_id=style_profile_id,
        sanitized_summary=thread_record.sanitized_thread_summary,
        conversation_pattern=_conversation_pattern(messages),
        effective_phrases=_effective_phrases(text),
        cta_pattern=_cta_pattern(text),
        follow_up_pattern=_follow_up_pattern(messages),
        reply_pattern=_reply_pattern(messages),
        lessons_learned=_lessons(messages),
        approved_for_drafting=approved_for_drafting,
        raw_body_included=False,
        message_summaries=messages,
        metadata={
            **dict(metadata or {}),
            "sensitive_flags": sensitive_flags,
            "message_count": len(messages),
            "capture_mode": "sanitized_summary_only",
        },
    )


def outreach_example_memory_item(example: OutreachExampleRecord | Mapping[str, Any]) -> Any:
    """Build a prompt-safe memory item from a sanitized outreach example record."""

    from keystone_agents.schemas.approval import ApprovalState
    from keystone_agents.schemas.memory import MemoryItem, normalize_memory_key

    record = (
        example
        if isinstance(example, OutreachExampleRecord)
        else OutreachExampleRecord.model_validate(example)
    )
    return MemoryItem(
        memory_type="outreach_example",
        object_type="outreach_draft",
        object_id=record.example_id,
        object_key=normalize_memory_key(record.template_id or record.example_id),
        title=f"Outreach example: {record.example_id}",
        summary=record.conversation_pattern,
        content=record.model_dump(mode="json"),
        source_ids=record.source_ids,
        approval_state=(
            ApprovalState.APPROVED_FOR_DRAFTING
            if record.approved_for_drafting
            else ApprovalState.PENDING
        ),
        confidence=0.8 if record.approved_for_drafting else 0.2,
        sensitivity="internal",
        safe_for_prompt=True,
        metadata={"source": "sanitized_outreach_example"},
        raw_email_body_included=False,
        send_enabled=False,
        sent=False,
    )


def _token_set(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) > 1 and token not in RETRIEVAL_STOPWORDS
    }


def _private_content_present(record: OutreachExampleRecord) -> bool:
    text = record.retrieval_text()
    if RAW_HEADER_RE.search(text):
        return True
    sanitized, flags = sanitize_outreach_example_text(text)
    lowered = sanitized.lower()
    return bool(flags) or any(marker in lowered for marker in PRIVATE_RETRIEVAL_MARKERS)


def _retrieval_score(
    record: OutreachExampleRecord,
    *,
    outreach_goal: str,
    company_type: str,
    opportunity_type: str,
    selected_template: str,
    outreach_stage: str,
) -> tuple[int, str]:
    score = 0
    reasons: list[str] = []
    query_tokens = _token_set(
        " ".join([outreach_goal, company_type, opportunity_type, selected_template, outreach_stage])
    )
    record_tokens = _token_set(record.retrieval_text())
    overlap = len(query_tokens & record_tokens)
    if overlap:
        score += overlap
        reasons.append("semantic token match")
    if selected_template and selected_template == (record.template_id or ""):
        score += 10
        reasons.append("template match")
    if outreach_stage and outreach_stage in record.outreach_stages:
        score += 6
        reasons.append("stage match")
    if company_type and company_type in record.company_types:
        score += 5
        reasons.append("company type match")
    if opportunity_type and opportunity_type in record.opportunity_types:
        score += 5
        reasons.append("opportunity type match")
    return score, ", ".join(reasons) or "approved sanitized example"


def _retrieved_record(
    record: OutreachExampleRecord,
    *,
    score: int,
    reason: str,
) -> RetrievedOutreachExample:
    return RetrievedOutreachExample(
        example_id=record.example_id,
        match_score=score,
        match_reason=reason,
        conversation_pattern=record.conversation_pattern,
        effective_phrases=record.effective_phrases,
        cta_pattern=record.cta_pattern,
        follow_up_pattern=record.follow_up_pattern,
        reply_pattern=record.reply_pattern,
        lessons_learned=record.lessons_learned,
        template_id=record.template_id,
        style_profile_id=record.style_profile_id,
        raw_body_included=False,
    )


def retrieve_outreach_examples_local(
    *,
    outreach_goal: str = "",
    company_type: str = "",
    opportunity_type: str = "",
    selected_template: str = "",
    outreach_stage: str = "",
    limit: int = 3,
    database_url: str | None = None,
) -> OutreachExampleRetrievalResult:
    """Retrieve approved sanitized memory-backed outreach examples."""

    from keystone_agents.storage.sqlite_store import SQLiteStore

    store = SQLiteStore(database_url)
    query = " ".join(
        [outreach_goal, company_type, opportunity_type, selected_template, outreach_stage]
    )
    bounded_limit = max(0, min(int(limit or 3), 3))
    items = store.retrieve_memory(
        query,
        memory_types=["outreach_example"],
        limit=max(bounded_limit * 3, 1),
        approved_only=True,
        safe_for_prompt=True,
    )
    ranked: list[tuple[int, str, RetrievedOutreachExample]] = []
    for item in items:
        try:
            record = OutreachExampleRecord.model_validate(item.content)
        except ValueError:
            continue
        if not record.approved_for_drafting or record.raw_body_included:
            continue
        if _private_content_present(record):
            continue
        score, reason = _retrieval_score(
            record,
            outreach_goal=outreach_goal,
            company_type=company_type,
            opportunity_type=opportunity_type,
            selected_template=selected_template,
            outreach_stage=outreach_stage,
        )
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                record.example_id,
                _retrieved_record(record, score=score, reason=reason),
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]))
    records = [record for _, _, record in ranked[:bounded_limit]]
    guidance = (
        "Use approved sanitized examples as style and structure guidance only: "
        + "; ".join(f"{record.example_id} ({record.match_reason})" for record in records)
        if records
        else "No approved sanitized outreach examples matched the request."
    )
    return OutreachExampleRetrievalResult(
        query=query,
        approved_only=True,
        records=records,
        guidance=guidance,
        raw_body_included=False,
        embeddings_used=False,
        external_vector_db_used=False,
        send_enabled=False,
    )
