"""Model-visible bounded Gmail schema, query, and context tools.

These tools expose the existing Gmail provider boundary without returning raw
message bodies or permitting writes. Live reads require both an explicit tool
argument and the repository's existing Gmail live gate.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Annotated, Any, Literal

from pydantic import Field

from keystone_agents.guardrails import (
    keystone_tool_guardrail_kwargs,
    redact_secret_like_text,
)
from keystone_agents.runtime.request_budget import current_model_request_capacity
from keystone_agents.schemas.gmail_query import (
    MAX_GMAIL_MODEL_QUERIES,
    GmailExecutionParameterSchema,
    GmailMailboxReadSchema,
    GmailMailboxResourceSchema,
    GmailMessageQueryResult,
    GmailMessageSummaryRecord,
    GmailQueryParameterSchema,
    GmailReadContextResult,
    GmailSafeAttachmentMetadata,
    GmailSourceBodyEvidence,
)
from keystone_agents.sdk import function_tool
from keystone_agents.tools import gmail_tool

GMAIL_MODEL_QUERY_MAX_RESULTS = 20
GMAIL_MODEL_CONTEXT_READ_MAX = 4
GMAIL_LIVE_READ_ENV = "KEYSTONE_ENABLE_LIVE_GMAIL"
GMAIL_FIXTURE_MESSAGE_ID = "fixture-gmail-message-001"
GMAIL_FIXTURE_THREAD_ID = "fixture-gmail-thread-001"
_GMAIL_EVIDENCE_LEDGER_ATTR = "_keystone_gmail_evidence_ledger"
_GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS = 2
_GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS = 3


@dataclass
class GmailModelReadEvidenceLedger:
    """Request-local, model-visible Gmail evidence retained across SDK attempts."""

    _entries: list[dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)

    def record(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        output: Mapping[str, Any],
    ) -> None:
        with self._lock:
            self._entries.append(
                {
                    "tool_name": str(tool_name),
                    "arguments": dict(arguments),
                    "output": dict(output),
                    "invocation_index": len(self._entries) + 1,
                }
            )

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(
                json.loads(json.dumps(entry, ensure_ascii=True, sort_keys=True))
                for entry in self._entries
            )


def gmail_model_read_evidence_snapshot(
    tools: Any,
) -> tuple[dict[str, Any], ...]:
    """Return the cumulative bounded Gmail evidence attached to one tool set."""

    for tool in tools or ():
        ledger = getattr(tool, _GMAIL_EVIDENCE_LEDGER_ATTR, None)
        if isinstance(ledger, GmailModelReadEvidenceLedger):
            return ledger.snapshot()
    return ()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _gmail_model_request_capacity_payload(*, item_count: int) -> dict[str, Any]:
    """Return dynamic, model-visible capacity after one Gmail query call."""

    capacity = current_model_request_capacity(load_environment=False)
    if capacity is None:
        return {
            "schema_name": "keystone.gmail.model_request_capacity.v1",
            "configured": False,
            "remaining_model_requests": None,
            "reserved_final_response_requests": 1,
            "required_future_requests": (
                _GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS
                if item_count
                else _GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS
            ),
            "required_requests_for_candidate_read_and_final": (
                _GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS
            ),
            "required_requests_for_corrective_query_read_and_final": (
                _GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS
            ),
            "candidate_read_and_final_allowed": bool(item_count),
            "corrective_query_allowed": True,
            "final_response_allowed": True,
            "status": "not_configured",
        }
    remaining = capacity.remaining
    candidate_read_allowed = bool(
        item_count
        and (
            remaining is None
            or remaining >= _GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS
        )
    )
    corrective_query_allowed = bool(
        remaining is None
        or remaining >= _GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS
    )
    final_response_allowed = bool(remaining is None or remaining >= 1)
    if candidate_read_allowed:
        status = "candidate_read_and_final_admitted"
    elif not item_count and corrective_query_allowed:
        status = "corrective_query_read_and_final_admitted"
    elif final_response_allowed:
        status = "final_response_only"
    else:
        status = "exhausted"
    capacity_receipt = capacity.receipt(
        required_future_requests=(
            _GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS
            if item_count
            else _GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS
        )
    )
    capacity_receipt.pop("schema", None)
    return {
        **capacity_receipt,
        "schema_name": "keystone.gmail.model_request_capacity.v1",
        "required_requests_for_candidate_read_and_final": (
            _GMAIL_CONTEXT_READ_AND_FINAL_REQUESTS
        ),
        "required_requests_for_corrective_query_read_and_final": (
            _GMAIL_CORRECTIVE_QUERY_READ_AND_FINAL_REQUESTS
        ),
        "candidate_read_and_final_allowed": candidate_read_allowed,
        "corrective_query_allowed": corrective_query_allowed,
        "final_response_allowed": final_response_allowed,
        "status": status,
    }


def _gmail_final_response_capacity_payload() -> dict[str, Any]:
    """Return the final-response reserve visible after one Gmail context read."""

    capacity = current_model_request_capacity(load_environment=False)
    if capacity is None:
        return {
            "schema_name": "keystone.gmail.model_request_capacity.v1",
            "configured": False,
            "remaining_model_requests": None,
            "reserved_final_response_requests": 1,
            "required_future_requests": 1,
            "final_response_allowed": True,
            "must_return_final_response_now": False,
            "status": "not_configured",
        }
    remaining = capacity.remaining
    final_allowed = bool(remaining is None or remaining >= 1)
    capacity_receipt = capacity.receipt(required_future_requests=1)
    capacity_receipt.pop("schema", None)
    return {
        **capacity_receipt,
        "schema_name": "keystone.gmail.model_request_capacity.v1",
        "final_response_allowed": final_allowed,
        "must_return_final_response_now": remaining == 1,
        "status": "final_response_admitted" if final_allowed else "exhausted",
    }


def _clean_query_value(value: str, *, name: str, max_length: int) -> str:
    cleaned = " ".join(str(value or "").split()).strip()
    if len(cleaned) > max_length:
        raise ValueError(f"{name} must be at most {max_length} characters.")
    return cleaned


def _safe_gmail_text(value: Any) -> str:
    """Keep bounded Gmail evidence while removing credential-shaped fragments."""

    return redact_secret_like_text(value)


def _safe_gmail_text_list(values: Any, *, max_items: int) -> list[str]:
    if isinstance(values, str) or not isinstance(values, list | tuple):
        return []
    return [_safe_gmail_text(item) for item in values[:max_items]]


def _safe_gmail_links(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list | tuple):
        return []
    links = []
    for item in values[:10]:
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if _safe_gmail_text(url) != url:
            continue
        links.append({
            "url": url, "suspicious": item.get("suspicious") is True,
            "reasons": _safe_gmail_text_list(item.get("reasons", []), max_items=10),
        })
    return links


def _safe_gmail_body_evidence(
    values: Any,
    *,
    message_id: str,
    thread_id: str,
    received_at: str,
    sender_name: str,
    sender_email: str,
    max_items: int,
    max_chars: int,
) -> tuple[list[GmailSourceBodyEvidence], bool]:
    if isinstance(values, str) or not isinstance(values, list | tuple):
        return [], False
    output: list[GmailSourceBodyEvidence] = []
    remaining = max_chars
    truncated = False
    for item in values:
        if len(output) >= max_items:
            truncated = True
            break
        if not isinstance(item, Mapping):
            continue
        source_text = _safe_gmail_text(item.get("source_text"))
        item_truncated = item.get("truncated") is True
        item_limit = min(remaining, 3000)
        if len(source_text) > item_limit:
            source_text = source_text[:item_limit]
            item_truncated = True
            truncated = True
        remaining = max(0, remaining - len(source_text))
        if not remaining and any(
            isinstance(other, Mapping) and other.get("source_text")
            for other in values[len(output) + 1 :]
        ):
            truncated = True
        raw_quotations = item.get("quotation_metadata") or item.get("quotations") or []
        quotation_metadata = [
            {
                "quote_kind": quote_item.get("quote_kind"),
                "attribution": _safe_gmail_text(quote_item.get("attribution")),
                "attribution_status": quote_item.get("attribution_status", "unknown"),
                "truncated": quote_item.get("truncated") is True,
            }
            for quote_item in raw_quotations
            if isinstance(quote_item, Mapping)
        ][:20]
        try:
            output.append(
                GmailSourceBodyEvidence(
                    message_id=message_id,
                    thread_id=thread_id,
                    received_at=received_at,
                    sender_name=sender_name,
                    sender_email=sender_email,
                    part_path=item.get("part_path", "unknown"),
                    mime_type=item.get("mime_type", "text/plain"),
                    container_mime_type=item.get("container_mime_type", ""),
                    alternative_group=item.get("alternative_group", ""),
                    representation=item.get("representation", "other_text"),
                    role=item.get("role", "alternate_representation"),
                    source_text=source_text,
                    quotation_metadata=quotation_metadata,
                    structure_annotations=item.get("structure_annotations") is True,
                    content_complete=(
                        item.get("content_complete") is True and not item_truncated
                    ),
                    truncated=item_truncated,
                    coverage=item.get("coverage"),
                    next_request=item.get("next_request"),
                    limitations=_safe_gmail_text_list(
                        item.get("limitations", []),
                        max_items=10,
                    ),
                )
            )
        except (TypeError, ValueError):
            truncated = True
    return output, truncated


def _secret_redaction_applied(value: Any) -> bool:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return redact_secret_like_text(serialized) != serialized


def _require_live_gmail_read(live: bool) -> None:
    if not live:
        return
    enabled = str(os.getenv(GMAIL_LIVE_READ_ENV) or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        raise gmail_tool.GmailConfigurationError(
            f"Model-visible Gmail reads require live=true and {GMAIL_LIVE_READ_ENV}=true."
        )


def inspect_gmail_mailbox_schema_impl() -> GmailMailboxReadSchema:
    """Return the stable schema and safety boundary for Gmail read tools."""

    return GmailMailboxReadSchema(
        query_parameters=["query", "label", "max_results"],
        query_parameter_details=[
            GmailQueryParameterSchema(
                name="query",
                purpose=(
                    "Optional Gmail provider search expression used to narrow the bounded "
                    "message-summary read."
                ),
                constraints=[
                    "At most 500 characters", "Does not return raw message bodies",
                    "The connection selects the mailbox; account names are not sender filters.",
                    "Use from: only for a supplied or evidenced message sender.",
                ],
            ),
            GmailQueryParameterSchema(
                name="label",
                purpose=(
                    "Optional exact provider label ID or Gmail system label used to scope "
                    "the summary search."
                ),
                constraints=["At most 100 characters", "Custom display names are not resolved"],
            ),
            GmailQueryParameterSchema(
                name="max_results",
                purpose="Maximum number of bounded message-summary records to return.",
                constraints=["Integer from 1 through 20", "Defaults to 10"],
            ),
        ],
        execution_parameter_details=[
            GmailExecutionParameterSchema(
                name="live",
                purpose=(
                    "Whether to perform the authenticated Gmail provider read instead "
                    "of returning a synthetic fixture."
                ),
                constraints=[
                    "Boolean; defaults to false",
                    "Requires an operator-requested read and KEYSTONE_ENABLE_LIVE_GMAIL=true",
                    "Never authorizes mailbox writes or sends",
                ],
            )
        ],
        resources=[
            GmailMailboxResourceSchema(
                resource_type="message_summary",
                identity_fields=["message_id", "thread_id"],
                returned_fields=[
                    "received_at",
                    "sender_name",
                    "sender_email",
                    "subject",
                    "snippet",
                    "prior_labels",
                ],
                omitted_fields=["raw MIME", "complete message body", "attachments"],
            ),
            GmailMailboxResourceSchema(
                resource_type="message_context",
                identity_fields=["message_id", "thread_id"],
                returned_fields=[
                    "message summary",
                    "bounded sanitized selected-message text windows",
                    "MIME/source evidence with quotation provenance and exact coverage",
                    "snapshot-pinned next requests for later inline text",
                    "account, message, thread, provider-history and sanitized-snapshot identity",
                    "account-scoped Gmail source URL",
                    "suspicious_signals",
                    "attachment_metadata",
                    "triage_limitations",
                ],
                omitted_fields=[
                    "raw MIME",
                    "attachment-backed body content",
                    "image or media content",
                    "recipient headers",
                ],
            ),
            GmailMailboxResourceSchema(
                resource_type="thread_context",
                identity_fields=["thread_id"],
                returned_fields=[
                    "message_count",
                    "dated per-message summaries (up to 12, with sender and snippet)",
                    "bounded source evidence with message, sender, and time boundaries",
                    "account-scoped Gmail source URL",
                    "subject",
                    "summary",
                    "thread_context",
                    "participants",
                    "action_items",
                    "deadlines",
                    "open_questions",
                    "triage_limitations",
                ],
                omitted_fields=["raw MIME", "per-message complete bodies"],
            ),
        ],
    )


def _fixture_summary() -> GmailMessageSummaryRecord:
    return GmailMessageSummaryRecord(
        message_id=GMAIL_FIXTURE_MESSAGE_ID,
        thread_id=GMAIL_FIXTURE_THREAD_ID,
        received_at="2026-01-15T15:00:00Z",
        sender_name="Synthetic Partner",
        sender_email="partner@example.com",
        subject="Synthetic partnership follow-up",
        snippet="Could we schedule a short discussion about the proposed workflow?",
        prior_labels=["INBOX", "UNREAD"],
    )


def _summary_from_provider(value: Mapping[str, Any]) -> GmailMessageSummaryRecord | None:
    message_id = str(value.get("id") or value.get("message_id") or "").strip()
    thread_id = str(value.get("threadId") or value.get("thread_id") or "").strip()
    if not message_id or not thread_id:
        return None
    snippet = " ".join(_safe_gmail_text(value.get("snippet")).split())[:500]
    return GmailMessageSummaryRecord(
        message_id=message_id,
        thread_id=thread_id,
        received_at=str(value.get("received_at") or ""),
        sender_name=_safe_gmail_text(value.get("sender_name")),
        sender_email=_safe_gmail_text(value.get("sender_email") or value.get("from")),
        subject=_safe_gmail_text(value.get("subject")),
        snippet=snippet,
        prior_labels=[
            str(item)
            for item in (value.get("prior_labels") or value.get("labelIds") or [])
            if str(item).strip()
        ],
    )


def _quoted_anchor_query(query: str) -> str | None:
    """Relax only unqualified clue text; preserve explicit Gmail filter syntax."""
    unquoted = re.sub(r'"[^"\n]*"', " ", query)
    if (
        query.count('"') % 2
        or "\\" in query
        or any(char in unquoted for char in "{}()*")
        or re.search(r"\b[\w-]+:|(?:^|\s)[+-]\S|\b(?:OR|AND|NOT|AROUND)\b", unquoted, re.I)
    ):
        return None
    anchors = re.findall(r'"([^"\n]+)"', query)
    if not anchors or any(len(re.findall(r"\w+", anchor)) < 2 for anchor in anchors):
        return None
    relaxed = " ".join(f'"{anchor}"' for anchor in anchors)
    return relaxed if relaxed != query.strip() else None


def query_gmail_message_summaries_impl(
    *,
    query: str = "",
    label: str = "",
    max_results: int = 10,
    live: bool = False,
) -> GmailMessageQueryResult:
    """Query bounded Gmail summaries through the existing provider adapter."""

    clean_query = _clean_query_value(query, name="query", max_length=500)
    clean_label = _clean_query_value(label, name="label", max_length=100)
    if not 1 <= max_results <= GMAIL_MODEL_QUERY_MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {GMAIL_MODEL_QUERY_MAX_RESULTS}.")
    _require_live_gmail_read(live)
    if not live:
        fixture = _fixture_summary()
        return GmailMessageQueryResult(
            status="fixture",
            query=clean_query,
            label=clean_label,
            requested_max_results=max_results,
            item_count=1,
            items=[fixture],
            limitations=[
                "Synthetic fixture only; no Gmail provider read was performed.",
                "Use live=true only when the operator requested Gmail access and the "
                "live gate is enabled.",
            ],
        )

    values = gmail_tool.search_message_summaries(
        label=clean_label or None,
        max_results=max_results,
        query=clean_query or None,
    )
    executed_queries = [clean_query]
    anchor_query = (
        _quoted_anchor_query(clean_query) if isinstance(values, list) and not values else None
    )
    if anchor_query:
        values = gmail_tool.search_message_summaries(
            label=clean_label or None, max_results=max_results, query=anchor_query,
        )
        executed_queries.append(anchor_query)
    items = [
        summary
        for value in values[:max_results]
        if isinstance(value, Mapping) and (summary := _summary_from_provider(value)) is not None
    ]
    return GmailMessageQueryResult(
        status="read",
        query=clean_query,
        executed_queries=executed_queries,
        label=clean_label,
        requested_max_results=max_results,
        item_count=len(items),
        items=items,
        provider_read_performed=True,
        limitations=[
            "Metadata and snippets only; complete message bodies were not returned.",
            "Use an exact returned message_id or thread_id for bounded follow-up context.",
            *([
                "The original query returned no matches. One bounded recovery search "
                "retained its quoted anchor phrases and removed other free-text qualifiers. "
                "Both executed queries are listed; the requested label scope is unchanged. "
                "Returned candidates may not match every original clue: verify the selected "
                "message against the operator request before using it."
            ] if anchor_query else []),
            *([
                "No messages matched this query; this does not establish that "
                "the requested email is absent.",
                "The connected mailbox is separate from the sender. "
                "Check unsupported from: or label filters.",
                "Use the remaining bounded queries to drop uncertain qualifiers; "
                "do not invent a sender.",
            ] if not items else []),
        ],
    )


def _fixture_read_context(
    resource_type: Literal["message", "thread"],
    resource_id: str,
) -> GmailReadContextResult:
    expected_id = (
        GMAIL_FIXTURE_MESSAGE_ID if resource_type == "message" else GMAIL_FIXTURE_THREAD_ID
    )
    if resource_id != expected_id:
        return GmailReadContextResult(
            status="not_found",
            resource_type=resource_type,
            resource_id=resource_id,
            triage_limitations=[
                "Synthetic fixture mode recognizes only identities returned by the "
                "fixture query tool."
            ],
        )
    summary = _fixture_summary()
    if resource_type == "message":
        return GmailReadContextResult(
            status="fixture",
            resource_type="message",
            resource_id=resource_id,
            message=summary,
            message_count=1,
            subject=summary.subject,
            summary=summary.snippet,
            latest_received_at=summary.received_at,
            participants=[summary.sender_name],
            open_questions=[summary.snippet],
            triage_limitations=[
                "Synthetic fixture only; no Gmail provider read was performed.",
                "Complete message bodies are never returned by this tool.",
            ],
        )
    return GmailReadContextResult(
        status="fixture",
        resource_type="thread",
        resource_id=resource_id,
        message_count=1,
        subject=summary.subject,
        summary=summary.snippet,
        thread_context=summary.snippet,
        latest_received_at=summary.received_at,
        participants=[summary.sender_name],
        open_questions=[summary.snippet],
        triage_limitations=[
            "Synthetic fixture only; no Gmail provider read was performed.",
            "Complete per-message bodies are never returned by this tool.",
        ],
    )


def _message_context_from_provider(
    resource_id: str,
    value: Mapping[str, Any],
) -> GmailReadContextResult:
    summary = _summary_from_provider(value)
    if summary is None or summary.message_id != resource_id:
        return GmailReadContextResult(
            status="not_found",
            resource_type="message",
            resource_id=resource_id,
            provider_read_performed=True,
            triage_limitations=[
                "The Gmail provider did not return the exact requested message identity."
            ],
        )
    provider_status = str(value.get("status") or "read")
    if provider_status in {"out_of_range", "source_changed", "source_inaccessible"}:
        return GmailReadContextResult(
            status=provider_status,
            resource_type="message",
            resource_id=resource_id,
            thread_id=summary.thread_id,
            account_identity_sha256=str(
                value.get("account_identity_sha256") or ""
            ),
            provider_history_id=str(value.get("provider_history_id") or ""),
            source_snapshot_sha256=str(
                value.get("source_snapshot_sha256") or ""
            ),
            source_restart_required=value.get("source_restart_required") is True,
            message=summary,
            message_count=1,
            subject=summary.subject,
            source_url=str(value.get("source_url") or ""),
            body_content_status="partial",
            body_content_complete=False,
            triage_limitations=_safe_gmail_text_list(
                value.get("triage_limitations") or [],
                max_items=20,
            ),
            provider_read_performed=True,
        )
    attachment_metadata = [
        GmailSafeAttachmentMetadata.from_provider(item)
        for item in (value.get("attachment_metadata") or [])[:10]
        if isinstance(item, Mapping)
    ]
    redaction_applied = _secret_redaction_applied(
        {
            "summary": value.get("thread_summary") or value.get("snippet") or "",
            "thread_context": value.get("thread_context") or "",
            "body_evidence": value.get("body_evidence") or [],
            "suspicious_signals": value.get("suspicious_signals") or [],
            "triage_limitations": value.get("triage_limitations") or [],
        }
    )
    body_evidence, evidence_truncated = _safe_gmail_body_evidence(
        value.get("body_evidence") or [],
        message_id=summary.message_id,
        thread_id=summary.thread_id,
        received_at=summary.received_at,
        sender_name=summary.sender_name,
        sender_email=summary.sender_email,
        max_items=8,
        max_chars=6000,
    )
    limitations = [
        *_safe_gmail_text_list(
            value.get("triage_limitations") or [],
            max_items=18,
        ),
        "Only bounded sanitized selected-message text and source evidence are included; "
        "raw MIME bytes and attachments are omitted.",
    ]
    if evidence_truncated:
        limitations.append("Bounded selected-message source evidence was truncated.")
    if redaction_applied:
        limitations.append(
            "One or more credential-shaped values were redacted from the bounded "
            "message projection before model use."
        )
    body_content_status = str(value.get("body_content_status") or "empty")
    if redaction_applied and body_content_status == "complete":
        body_content_status = "partial"
    return GmailReadContextResult(
        status="read",
        resource_type="message",
        resource_id=resource_id,
        thread_id=summary.thread_id,
        account_identity_sha256=str(value.get("account_identity_sha256") or ""),
        provider_history_id=str(value.get("provider_history_id") or ""),
        source_snapshot_sha256=str(value.get("source_snapshot_sha256") or ""),
        source_restart_required=value.get("source_restart_required") is True,
        message=summary,
        message_count=1,
        subject=summary.subject,
        summary=_safe_gmail_text(value.get("thread_summary") or summary.snippet),
        thread_context=_safe_gmail_text(value.get("thread_context")),
        source_url=str(value.get("source_url") or ""),
        extracted_links=_safe_gmail_links(value.get("extracted_links", [])),
        latest_received_at=summary.received_at,
        participants=[_safe_gmail_text(summary.sender_name or summary.sender_email)],
        suspicious_signals=_safe_gmail_text_list(
            value.get("suspicious_signals") or [],
            max_items=10,
        ),
        attachment_metadata=attachment_metadata,
        body_evidence=body_evidence,
        body_content_status=body_content_status,
        body_content_complete=(
            value.get("body_content_complete") is True
            and not evidence_truncated
            and not redaction_applied
        ),
        triage_limitations=limitations,
        provider_read_performed=True,
    )


def _thread_context_from_provider(
    resource_id: str,
    value: Mapping[str, Any],
) -> GmailReadContextResult:
    returned_id = str(value.get("thread_id") or value.get("id") or "").strip()
    if not returned_id or returned_id != resource_id:
        return GmailReadContextResult(
            status="not_found",
            resource_type="thread",
            resource_id=resource_id,
            provider_read_performed=True,
            triage_limitations=[
                "The Gmail provider did not return the exact requested thread identity."
            ],
        )
    redaction_applied = _secret_redaction_applied(
        {
            "subject": value.get("subject") or "",
            "summary": value.get("summary") or "",
            "thread_context": value.get("thread_context") or "",
            "participants": value.get("participants") or [],
            "action_items": value.get("action_items") or [],
            "deadlines": value.get("deadlines") or [],
            "open_questions": value.get("open_questions") or [],
            "body_evidence": [
                message.get("body_evidence") or []
                for message in value.get("messages") or []
                if isinstance(message, Mapping)
            ],
            "triage_limitations": value.get("triage_limitations") or [],
        }
    )
    limitations = [
        *_safe_gmail_text_list(
            value.get("triage_limitations") or [],
            max_items=14,
        ),
        "Complete per-message bodies are intentionally omitted from model-visible tool output.",
    ]
    if redaction_applied:
        limitations.append(
            "One or more credential-shaped values were redacted from the bounded "
            "thread projection before model use."
        )
    links = list(value.get("extracted_links") or [])[:10]
    messages = [
        summary for message in value.get("messages") or []
        if isinstance(message, Mapping) and (summary := _summary_from_provider(message)) is not None
        and summary.thread_id == returned_id
    ]
    chronology_available = True
    try:
        messages = sorted(messages, key=lambda message: datetime.fromisoformat(
            message.received_at.replace("Z", "+00:00")
        ))
    except (ValueError, TypeError):
        chronology_available = False
        limitations.append("Some message dates are missing or invalid; chronology is incomplete.")
    if len(messages) > 12:
        limitations.append(
            "The per-message timeline includes only the most recent 12 messages."
            if chronology_available else
            "The per-message timeline is limited to 12 provider entries; their order is uncertain."
        )
    if len(messages) != int(value.get("message_count") or 0):
        limitations.append("Some thread messages are not represented in the timeline.")
    for message in (value.get("messages") or [])[:100]:
        if len(links) >= 10:
            break
        if isinstance(message, Mapping):
            links.extend(list(message.get("extracted_links") or [])[:10 - len(links)])
    raw_body_evidence: list[dict[str, Any]] = []
    content_statuses: list[str] = []
    represented_message_ids: set[str] = set()
    for message in value.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        message_id = str(message.get("id") or message.get("message_id") or "").strip()
        message_thread_id = str(
            message.get("threadId") or message.get("thread_id") or ""
        ).strip()
        if not message_id or message_thread_id != returned_id:
            continue
        status = str(message.get("body_content_status") or "empty")
        content_statuses.append(status)
        for item in message.get("body_evidence") or []:
            if not isinstance(item, Mapping):
                continue
            raw_body_evidence.append(
                {
                    **item,
                    "_message_id": message_id,
                    "_thread_id": message_thread_id,
                    "_received_at": str(message.get("received_at") or ""),
                    "_sender_name": _safe_gmail_text(message.get("sender_name")),
                    "_sender_email": _safe_gmail_text(
                        message.get("sender_email") or message.get("from")
                    ),
                }
            )
            represented_message_ids.add(message_id)
    body_evidence: list[GmailSourceBodyEvidence] = []
    evidence_truncated = False
    remaining_chars = 12_000
    for item in raw_body_evidence:
        if len(body_evidence) >= 16 or remaining_chars <= 0:
            evidence_truncated = True
            break
        projected, item_truncated = _safe_gmail_body_evidence(
            [item],
            message_id=str(item.get("_message_id") or ""),
            thread_id=str(item.get("_thread_id") or ""),
            received_at=str(item.get("_received_at") or ""),
            sender_name=str(item.get("_sender_name") or ""),
            sender_email=str(item.get("_sender_email") or ""),
            max_items=1,
            max_chars=min(3000, remaining_chars),
        )
        body_evidence.extend(projected)
        remaining_chars -= sum(len(entry.source_text) for entry in projected)
        evidence_truncated = evidence_truncated or item_truncated
    if len(represented_message_ids) < min(len(messages), 12):
        evidence_truncated = True
    if "conflicting" in content_statuses:
        body_content_status = "conflicting"
    elif evidence_truncated or redaction_applied or "partial" in content_statuses:
        body_content_status = "partial"
    elif content_statuses and all(status == "complete" for status in content_statuses):
        body_content_status = "complete"
    else:
        body_content_status = "empty"
    if evidence_truncated:
        limitations.append(
            "Thread source evidence is bounded; some representations or messages are omitted."
        )
    return GmailReadContextResult(
        status="read",
        resource_type="thread",
        resource_id=returned_id,
        message_count=min(int(value.get("message_count") or 0), 100),
        messages=messages[-12:],
        subject=_safe_gmail_text(value.get("subject")),
        summary=_safe_gmail_text(value.get("summary")),
        thread_context=_safe_gmail_text(value.get("thread_context")),
        source_url=str(value.get("source_url") or ""),
        extracted_links=_safe_gmail_links(links),
        latest_received_at=str(value.get("latest_received_at") or ""),
        participants=_safe_gmail_text_list(
            value.get("participants") or [],
            max_items=12,
        ),
        action_items=_safe_gmail_text_list(
            value.get("action_items") or [],
            max_items=5,
        ),
        deadlines=_safe_gmail_text_list(
            value.get("deadlines") or [],
            max_items=5,
        ),
        open_questions=_safe_gmail_text_list(
            value.get("open_questions") or [],
            max_items=5,
        ),
        body_evidence=body_evidence,
        body_content_status=body_content_status,
        body_content_complete=(
            body_content_status == "complete"
            and all(entry.content_complete for entry in body_evidence)
        ),
        triage_limitations=limitations,
        provider_read_performed=True,
    )


def read_gmail_context_impl(
    *,
    resource_type: Literal["message", "thread"],
    resource_id: str,
    body_part_path: str = "",
    body_start_char: int = 0,
    max_body_chars: int = 3_000,
    expected_thread_id: str = "",
    expected_account_identity_sha256: str = "",
    expected_source_snapshot_sha256: str = "",
    live: bool = False,
) -> GmailReadContextResult:
    """Read minimal safe context for one exact Gmail message or thread identity."""

    clean_id = str(resource_id or "").strip()
    if not clean_id or len(clean_id) > 200 or not clean_id.isascii():
        raise ValueError(
            "resource_id must be a non-empty ASCII Gmail identifier up to 200 characters."
        )
    clean_part_path = str(body_part_path or "").strip()
    if len(clean_part_path) > 120:
        raise ValueError("body_part_path must be at most 120 characters.")
    if body_start_char < 0:
        raise ValueError("body_start_char must be zero or greater.")
    if not 1 <= max_body_chars <= 3_000:
        raise ValueError("max_body_chars must be between 1 and 3000.")
    for name, value in (
        ("expected_account_identity_sha256", expected_account_identity_sha256),
        ("expected_source_snapshot_sha256", expected_source_snapshot_sha256),
    ):
        if value and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{name} must be an exact lowercase SHA-256 digest.")
    if expected_thread_id and (
        len(expected_thread_id) > 200 or not expected_thread_id.isascii()
    ):
        raise ValueError("expected_thread_id must be an ASCII Gmail identifier.")
    detail_requested = bool(
        clean_part_path
        or body_start_char
        or max_body_chars != 3_000
        or expected_thread_id
        or expected_account_identity_sha256
        or expected_source_snapshot_sha256
    )
    if resource_type == "thread" and detail_requested:
        raise ValueError(
            "Sanitized body continuation is available only for one exact message identity."
        )
    _require_live_gmail_read(live)
    if not live:
        return _fixture_read_context(resource_type, clean_id)
    if resource_type == "message":
        if not detail_requested:
            projection = gmail_tool.get_message_context_projection(clean_id)
        else:
            projection = gmail_tool.get_message_context_projection(
                clean_id,
                body_part_path=clean_part_path,
                body_start_char=body_start_char,
                max_body_chars=max_body_chars,
                expected_thread_id=expected_thread_id,
                expected_account_identity_sha256=expected_account_identity_sha256,
                expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            )
        return _message_context_from_provider(
            clean_id,
            projection,
        )
    return _thread_context_from_provider(clean_id, gmail_tool.get_thread_with_source_url(clean_id))


@function_tool(**keystone_tool_guardrail_kwargs())
def inspect_gmail_mailbox_schema() -> str:
    """Describe the bounded Gmail query and exact-context read contract."""

    return _json(inspect_gmail_mailbox_schema_impl().model_dump(mode="json"))


@function_tool(**keystone_tool_guardrail_kwargs())
def query_gmail_message_summaries(
    query: Annotated[str, Field(max_length=500)] = "",
    label: Annotated[
        str,
        Field(
            max_length=100,
            description=(
                "Leave empty to search all labels, including archived and sent mail. "
                "Use a provider label ID only if requested; custom names are not resolved."
            ),
        ),
    ] = "",
    max_results: Annotated[int, Field(ge=1, le=20)] = 10,
    live: bool = False,
) -> str:
    """Query up to 20 summaries using an exact provider label ID or system label."""

    return _json(
        query_gmail_message_summaries_impl(
            query=query,
            label=label,
            max_results=max_results,
            live=live,
        ).model_dump(mode="json")
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def read_gmail_context(
    resource_type: Literal["message", "thread"],
    resource_id: Annotated[str, Field(min_length=1, max_length=200)],
    body_part_path: Annotated[str, Field(max_length=120)] = "",
    body_start_char: Annotated[int, Field(ge=0)] = 0,
    max_body_chars: Annotated[int, Field(ge=1, le=3_000)] = 3_000,
    expected_thread_id: Annotated[str, Field(max_length=200)] = "",
    expected_account_identity_sha256: Annotated[
        str,
        Field(pattern=r"^(?:|[0-9a-f]{64})$"),
    ] = "",
    expected_source_snapshot_sha256: Annotated[
        str,
        Field(pattern=r"^(?:|[0-9a-f]{64})$"),
    ] = "",
    live: bool = False,
) -> str:
    """Read minimal context for one exact Gmail message or thread; never return full bodies."""

    return _json(
        read_gmail_context_impl(
            resource_type=resource_type,
            resource_id=resource_id,
            body_part_path=body_part_path,
            body_start_char=body_start_char,
            max_body_chars=max_body_chars,
            expected_thread_id=expected_thread_id,
            expected_account_identity_sha256=expected_account_identity_sha256,
            expected_source_snapshot_sha256=expected_source_snapshot_sha256,
            live=live,
        ).model_dump(mode="json")
    )


def gmail_model_read_tools(*, live: bool) -> tuple[Any, Any, Any]:
    """Bind provider execution mode before exposing Gmail reads to the model.

    The model decides whether and how to query/read Gmail. Python decides whether
    the already-authorized run uses fixture or live provider execution. This keeps
    a model-defaulted ``live=false`` argument from silently replacing a requested
    provider read with synthetic evidence.
    """

    execution_mode = "live_provider" if live else "fixture"
    candidate_message_to_thread: dict[str, str] = {}
    candidate_thread_ids: set[str] = set()
    query_cache: dict[tuple[str, str, int], GmailMessageQueryResult] = {}
    query_cache_lock = Lock()
    context_cache: dict[tuple[Any, ...], GmailReadContextResult] = {}
    context_cache_lock = Lock()
    context_read_locks: dict[tuple[Any, ...], Lock] = {}
    context_read_slots: set[tuple[str, str]] = set()
    query_completed = False
    evidence_ledger = GmailModelReadEvidenceLedger()

    @function_tool(
        name_override="inspect_gmail_mailbox_schema",
        description_override=(
            "Describe the bounded Gmail query and exact-context read contract. "
            f"Execution mode is already bound by the runtime as {execution_mode}."
        ),
        **keystone_tool_guardrail_kwargs(),
    )
    def bound_inspect_gmail_mailbox_schema() -> str:
        payload = inspect_gmail_mailbox_schema_impl().model_dump(mode="json")
        payload["execution_parameters"] = []
        payload["execution_parameter_details"] = []
        payload["live_argument_required"] = False
        if live:
            payload.pop("dry_run_mode", None)
        payload["execution_mode"] = execution_mode
        payload["execution_mode_note"] = (
            "Python bound this mode before the agent run; it is not a model tool argument."
        )
        return _json(payload)

    @function_tool(
        name_override="query_gmail_message_summaries",
        description_override=(
            "Query up to 20 bounded Gmail message summaries using a concise Gmail "
            "search expression built from stable sender, domain, subject, and time "
            "anchors. The connection selects the mailbox: an account name is not a "
            "sender and must not become an inferred from: filter. Use supplied subject "
            "terms and date bounds; omit labels unless requested. "
            "If the first result is empty or insufficient, make up to two "
            "distinct broader or corrected queries; never repeat the same query. "
            "An empty free-text query may receive one disclosed quoted-anchor "
            "recovery search; explicit Gmail filters are never relaxed. "
            f"Provider execution is already bound by the runtime as {execution_mode}."
        ),
        **keystone_tool_guardrail_kwargs(),
    )
    def bound_query_gmail_message_summaries(
        query: Annotated[
            str,
            Field(
                max_length=500,
                description=(
                    "Concise Gmail search expression using stable sender, domain, "
                    "subject, or time anchors; omit narrative and exclusion clauses. "
                    "Do not put the mailbox account name in search terms or from:. "
                    "Space-separated terms are ANDed: use a few distinctive clues, "
                    "not the whole remembered description. Use from: only for a "
                    "supplied or evidenced sender; subject terms can stand alone."
                ),
            ),
        ] = "",
        label: Annotated[
            str,
            Field(
                max_length=100,
                description=(
                    "Leave empty to search all labels, including archived and sent mail. "
                    "Use a provider label ID only if requested; custom names are not resolved."
                ),
            ),
        ] = "",
        max_results: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> str:
        nonlocal query_completed
        clean_query = _clean_query_value(query, name="query", max_length=500)
        clean_label = _clean_query_value(label, name="label", max_length=100)
        signature = (clean_query, clean_label, max_results)
        with query_cache_lock:
            cached_result = query_cache.get(signature)
            if cached_result is not None:
                cached_payload = cached_result.model_dump(mode="json")
                cached_payload["provider_read_performed"] = False
                cached_payload["remaining_query_calls"] = MAX_GMAIL_MODEL_QUERIES - len(query_cache)
                cached_payload["query_cache_reused"] = True
                cached_payload["model_request_capacity"] = (
                    _gmail_model_request_capacity_payload(
                        item_count=cached_result.item_count,
                    )
                )
                cached_payload["limitations"] = list(
                    dict.fromkeys(
                        [
                            *cached_payload.get("limitations", []),
                            (
                                "This exact query already ran in the current agent "
                                "loop. Its bounded result was reused without another "
                                "provider read. If evidence is insufficient, make one "
                                "distinct corrective query using stable sender, domain, "
                                "subject, or time anchors."
                            ),
                        ]
                    )
                )
                evidence_ledger.record(
                    tool_name="query_gmail_message_summaries_repeated",
                    arguments={
                        "query": clean_query,
                        "label": clean_label,
                        "max_results": max_results,
                    },
                    output=cached_payload,
                )
                return _json(cached_payload)
            if len(query_cache) >= MAX_GMAIL_MODEL_QUERIES:
                blocked_payload = GmailMessageQueryResult(
                    status="read" if live else "fixture",
                    query=clean_query,
                    label=clean_label,
                    requested_max_results=max_results,
                    item_count=0,
                    items=[],
                    limitations=[
                        "The three-query ceiling for this agent loop is already exhausted. "
                        "No additional provider read was performed. Decide from the "
                        "existing bounded evidence or request more context."
                    ],
                ).model_dump(mode="json")
                blocked_payload["query_budget_blocked"] = True
                blocked_payload["model_request_capacity"] = (
                    _gmail_model_request_capacity_payload(item_count=0)
                )
                evidence_ledger.record(
                    tool_name="query_gmail_message_summaries_blocked",
                    arguments={
                        "query": clean_query,
                        "label": clean_label,
                        "max_results": max_results,
                    },
                    output=blocked_payload,
                )
                return _json(blocked_payload)
            result = query_gmail_message_summaries_impl(
                query=clean_query,
                label=clean_label,
                max_results=max_results,
                live=live,
            )
            query_cache[signature] = result.model_copy(deep=True)
        candidate_message_to_thread.update(
            {item.message_id: item.thread_id for item in result.items}
        )
        candidate_thread_ids.update(item.thread_id for item in result.items)
        query_completed = True
        payload = result.model_dump(mode="json")
        payload["remaining_query_calls"] = MAX_GMAIL_MODEL_QUERIES - len(query_cache)
        payload["model_request_capacity"] = _gmail_model_request_capacity_payload(
            item_count=result.item_count,
        )
        evidence_ledger.record(
            tool_name="query_gmail_message_summaries",
            arguments={
                "query": clean_query,
                "label": clean_label,
                "max_results": max_results,
            },
            output=payload,
        )
        return _json(payload)

    @function_tool(
        name_override="read_gmail_context",
        description_override=(
            "Read minimal context for one exact Gmail message or thread. At most four "
            "distinct contexts may be read in this agent loop. If that ceiling is "
            "reached, decide from the four verified contexts already returned. Follow "
            "an exact returned body-evidence next_request when later sanitized text is "
            "needed; it stays on the same message, MIME part, account, thread, and source "
            "snapshot. Provider "
            f"execution is already bound by the runtime as {execution_mode}."
        ),
        **keystone_tool_guardrail_kwargs(),
    )
    def bound_read_gmail_context(
        resource_type: Literal["message", "thread"],
        resource_id: Annotated[str, Field(min_length=1, max_length=200)],
        body_part_path: Annotated[str, Field(max_length=120)] = "",
        body_start_char: Annotated[int, Field(ge=0)] = 0,
        max_body_chars: Annotated[int, Field(ge=1, le=3_000)] = 3_000,
        expected_thread_id: Annotated[str, Field(max_length=200)] = "",
        expected_account_identity_sha256: Annotated[
            str,
            Field(pattern=r"^(?:|[0-9a-f]{64})$"),
        ] = "",
        expected_source_snapshot_sha256: Annotated[
            str,
            Field(pattern=r"^(?:|[0-9a-f]{64})$"),
        ] = "",
    ) -> str:
        requested_id = str(resource_id or "").strip()
        effective_type = resource_type
        effective_id = requested_id
        normalized_from_message = False
        if query_completed and resource_type == "thread":
            if requested_id in candidate_message_to_thread:
                effective_id = candidate_message_to_thread[requested_id]
                normalized_from_message = True
            elif requested_id not in candidate_thread_ids:
                result = GmailReadContextResult(
                    status="not_found",
                    resource_type="thread",
                    resource_id=requested_id,
                    triage_limitations=[
                        "The requested thread identity was not returned by the "
                        "bounded Gmail query; no provider context read was performed."
                    ],
                )
                payload = result.model_dump(mode="json")
                evidence_ledger.record(
                    tool_name="read_gmail_context_rejected",
                    arguments={"resource_type": resource_type, "resource_id": requested_id},
                    output=payload,
                )
                return _json(payload)
        elif query_completed and resource_type == "message":
            if requested_id not in candidate_message_to_thread:
                result = GmailReadContextResult(
                    status="not_found",
                    resource_type="message",
                    resource_id=requested_id,
                    triage_limitations=[
                        "The requested message identity was not returned by the "
                        "bounded Gmail query; no provider context read was performed."
                    ],
                )
                payload = result.model_dump(mode="json")
                evidence_ledger.record(
                    tool_name="read_gmail_context_rejected",
                    arguments={"resource_type": resource_type, "resource_id": requested_id},
                    output=payload,
                )
                return _json(payload)
        cache_key = (
            effective_type,
            effective_id,
            body_part_path,
            body_start_char,
            max_body_chars,
            expected_thread_id,
            expected_account_identity_sha256,
            expected_source_snapshot_sha256,
        )
        context_slot_key = (effective_type, effective_id)
        with context_cache_lock:
            read_lock = context_read_locks.setdefault(cache_key, Lock())
        with read_lock:
            with context_cache_lock:
                cached = cache_key in context_cache
                cached_result = context_cache.get(cache_key)
                budget_blocked = (
                    not cached
                    and context_slot_key not in context_read_slots
                    and len(context_read_slots) >= GMAIL_MODEL_CONTEXT_READ_MAX
                )
                if not cached and not budget_blocked:
                    # Reserve the slot before the provider call so parallel distinct
                    # reads cannot cross the four-context ceiling. A failed provider
                    # attempt retains its slot because it still consumed a read attempt.
                    context_read_slots.add(context_slot_key)
            if budget_blocked:
                blocked_payload = GmailReadContextResult(
                    status="not_found",
                    resource_type=effective_type,
                    resource_id=effective_id,
                    triage_limitations=[
                        "The four-context Gmail read ceiling is already exhausted. "
                        "No additional provider read was performed. Decide using the "
                        "four verified contexts already returned, or explicitly state "
                        "that more operator context is needed."
                    ],
                ).model_dump(mode="json")
                blocked_payload.update(
                    {
                        "context_read_budget_blocked": True,
                        "recoverable": True,
                        "reason_code": "gmail_context_read_budget_exhausted",
                        "context_read_limit": GMAIL_MODEL_CONTEXT_READ_MAX,
                        "completed_context_read_count": len(context_read_slots),
                    }
                )
                evidence_ledger.record(
                    tool_name="read_gmail_context_blocked",
                    arguments={
                        "resource_type": resource_type,
                        "resource_id": requested_id,
                    },
                    output=blocked_payload,
                )
                return _json(blocked_payload)
            if cached and cached_result is not None:
                result = cached_result.model_copy(deep=True)
            else:
                result = read_gmail_context_impl(
                    resource_type=effective_type,
                    resource_id=effective_id,
                    body_part_path=body_part_path,
                    body_start_char=body_start_char,
                    max_body_chars=max_body_chars,
                    expected_thread_id=expected_thread_id,
                    expected_account_identity_sha256=expected_account_identity_sha256,
                    expected_source_snapshot_sha256=expected_source_snapshot_sha256,
                    live=live,
                )
                with context_cache_lock:
                    context_cache[cache_key] = result.model_copy(deep=True)
        limitations = list(result.triage_limitations)
        if normalized_from_message:
            limitations.append(
                "The model supplied a returned message_id as a thread identity; "
                "Python normalized it to that message's exact returned thread_id "
                "before the provider read."
            )
        if cached:
            limitations.append(
                "This exact context was already read in the current agent run; the "
                "bounded cached projection was reused without another provider read."
            )
        result = result.model_copy(update={"triage_limitations": list(dict.fromkeys(limitations))})
        payload = result.model_dump(mode="json")
        if cached:
            payload["provider_read_performed"] = False
            payload["context_cache_reused"] = True
        payload["model_request_capacity"] = _gmail_final_response_capacity_payload()
        evidence_ledger.record(
            tool_name=(
                "read_gmail_context_repeated" if cached else "read_gmail_context"
            ),
            arguments={
                "resource_type": resource_type,
                "resource_id": requested_id,
                "body_part_path": body_part_path,
                "body_start_char": body_start_char,
                "max_body_chars": max_body_chars,
                "expected_thread_id": expected_thread_id,
                "expected_account_identity_sha256": expected_account_identity_sha256,
                "expected_source_snapshot_sha256": expected_source_snapshot_sha256,
            },
            output=payload,
        )
        return _json(payload)

    tools = (
        bound_inspect_gmail_mailbox_schema,
        bound_query_gmail_message_summaries,
        bound_read_gmail_context,
    )
    for tool in tools:
        setattr(tool, _GMAIL_EVIDENCE_LEDGER_ATTR, evidence_ledger)
        sdk_tool = getattr(tool, "sdk_tool", None)
        if sdk_tool is not None:
            setattr(sdk_tool, _GMAIL_EVIDENCE_LEDGER_ATTR, evidence_ledger)
    return tools


__all__ = [
    "GMAIL_MODEL_CONTEXT_READ_MAX",
    "GMAIL_FIXTURE_MESSAGE_ID",
    "GMAIL_FIXTURE_THREAD_ID",
    "GMAIL_LIVE_READ_ENV",
    "GMAIL_MODEL_QUERY_MAX_RESULTS",
    "GmailModelReadEvidenceLedger",
    "gmail_model_read_evidence_snapshot",
    "gmail_model_read_tools",
    "inspect_gmail_mailbox_schema",
    "inspect_gmail_mailbox_schema_impl",
    "query_gmail_message_summaries",
    "query_gmail_message_summaries_impl",
    "read_gmail_context",
    "read_gmail_context_impl",
]
