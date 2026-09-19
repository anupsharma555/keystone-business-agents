"""Run Gmail triage with env-aware dry-run or live-test defaults."""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
import time as time_module
import unicodedata
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from keystone_agents.agents.gmail_triage import (
    GmailAgentDecisionError,
    build_gmail_contact_lookup_agent,
    build_gmail_priority_grouping_agent,
    build_gmail_triage_agent,
    email_fixture_to_envelope,
    load_email_fixture,
    run_gmail_triage_fixture,
    run_gmail_triage_sdk,
    triage_gmail_message_envelope,
)
from keystone_agents.cli_orchestrator_review import (
    add_orchestrator_review_arguments,
    build_cli_orchestrator_review,
)
from keystone_agents.cli_sdk import (
    SDKRunConfigFactory,
    add_sdk_run_arguments,
    reject_sdk_side_effect_flags,
    resolve_sdk_execution,
    sdk_agent_description,
    sdk_execution_requested,
    sdk_synthesis_payload,
)
from keystone_agents.config import (
    SLACK_APPROVAL_CREDENTIALS,
    cli_default_dry_run,
    cli_default_live_gmail,
    cli_default_live_sdk,
    require_cli_live_confirmation,
    with_cli_environment,
)
from keystone_agents.feedback import build_operator_feedback_request
from keystone_agents.founder_profile import (
    founder_drafting_context,
    founder_profile_audit_payload,
    load_founder_fit_profile,
)
from keystone_agents.gmail_triage.contact_lookup import (
    run_gmail_contact_lookup_workflow,
)
from keystone_agents.gmail_triage.draft_actions import (
    execute_approved_gmail_draft_action,
    execute_approved_gmail_draft_reply_action,
    resolve_unique_gmail_draft,
)
from keystone_agents.gmail_triage.execution_plan import extract_gmail_subject_hint
from keystone_agents.gmail_triage.priority_grouping import (
    rank_gmail_candidates_for_request,
)
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.models import (
    DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
)
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.orchestrator.preflight_context import (
    apply_orchestrator_preflight_to_args,
    attach_orchestrator_preflight_payload,
    load_specialist_execution_context_from_env,
    orchestrator_preflight_context_text,
)
from keystone_agents.presentation.public_result import attach_execution_public_result
from keystone_agents.presentation.renderers import render_gmail_selected_answer
from keystone_agents.reporting import (
    build_gmail_priority_grouping_test_pack_payload,
    render_gmail_priority_grouping_test_pack_report,
    render_gmail_triage_report,
    render_orchestrator_output_review,
    to_json,
)
from keystone_agents.run import (
    SDKSynthesisOutcome,
    run_retrieved_sdk_synthesis,
    sdk_run_failure_metadata,
)
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_triage import (
    EmailTriageResult,
    GmailClarificationResult,
    GmailMessageEnvelope,
    GmailPriorityGroupingResult,
    GmailThreadSummaryMessage,
    GmailThreadSummaryResult,
)
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.approval_tool import build_approval_queue_item, post_approval_request
from keystone_agents.tools.email_style_tool import (
    load_email_style_profile_fixture,
    load_email_style_profile_from_storage,
)
from keystone_agents.tools.gmail_tool import GmailAPIError, GmailConfigurationError, GmailTool
from keystone_agents.tools.storage_tool import StorageTool

APPROVAL_DECISIONS = [state.value for state in ApprovalState]
APPROVAL_SCOPES = [scope.value for scope in ApprovalScope]
TERMINAL_APPROVAL_DECISIONS = {ApprovalState.REJECTED.value, ApprovalState.EXPIRED.value}
SDK_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY: SDKRunConfigFactory | None = None
GT1_PRIORITY_GROUPING_PROMPT = DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST


class GmailTargetResolutionError(RuntimeError):
    """Carry a safe structured no-match result out of the SDK retrieval callback."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("human_summary") or payload.get("message") or ""))
        self.payload = payload


def _approval_context(
    *,
    object_id: str | int,
    summary: str,
    args: argparse.Namespace,
    slack_ts: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "approval_scope": args.approval_scope,
        "slack_ts": slack_ts,
        "send_enabled": False,
    }
    if args.ask_feedback:
        metadata["operator_feedback_request"] = build_operator_feedback_request(
            object_type="email_triage",
            object_id=object_id,
            source_agent="gmail_triage",
            review_stage="approval_review",
        ).model_dump(mode="json")
    return {
        "object_type": "gmail_draft",
        "object_id": object_id,
        "summary": summary,
        "decision": args.approval_decision,
        "scope": args.approval_scope,
        "reviewer": args.reviewer,
        "notes": args.approval_notes,
        "source_agent": "gmail_triage",
        "metadata": metadata,
    }


def _apply_labels_with_optional_cleanup(
    gmail: Any,
    *,
    message: dict[str, Any],
    message_id: str,
    labels: list[str],
    cleanup_obsolete: bool,
    dry_run_preview: bool,
) -> dict[str, Any]:
    optional_kwargs = {
        "existing_labels": [
            str(label) for label in message.get("labelIds", []) if str(label).strip()
        ],
        "cleanup_obsolete": cleanup_obsolete,
        "dry_run_preview": dry_run_preview,
    }
    signature = inspect.signature(gmail.apply_labels)
    parameters = signature.parameters
    supports_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if supports_kwargs or set(optional_kwargs) <= set(parameters):
        return gmail.apply_labels(message_id=message_id, labels=labels, **optional_kwargs)
    return gmail.apply_labels(message_id=message_id, labels=labels)


def _require_live_draft_approval(
    *,
    database_url: str | None,
    message_id: str,
) -> dict[str, Any]:
    """Require a latest approved-for-send local record before live draft creation."""

    approval = SQLiteStore(database_url).latest_approval(
        object_type="gmail_draft",
        object_id=message_id,
        scope=ApprovalScope.SEND,
    )
    expected = ApprovalState.APPROVED_FOR_SEND.value
    if approval is None:
        raise SystemExit(
            "Live Gmail draft creation requires an approved_for_send approval record "
            f"for gmail_draft '{message_id}' in local SQLite. "
            "Create or approve the record, pass --database-url if needed, and rerun. "
            "No Gmail draft was created."
        )
    decision = str(approval.get("decision") or "")
    scope = str(approval.get("scope") or "")
    if decision != expected or scope != ApprovalScope.SEND.value:
        raise SystemExit(
            "Live Gmail draft creation blocked: latest approval for "
            f"gmail_draft '{message_id}' is {decision or 'unknown'}/"
            f"{scope or 'unknown'}, expected {expected}/{ApprovalScope.SEND.value}. "
            "No Gmail draft was created."
        )
    return {
        "id": approval.get("id"),
        "object_type": approval.get("object_type"),
        "object_id": approval.get("object_id"),
        "decision": decision,
        "scope": scope,
        "reviewer": approval.get("reviewer") or "",
    }


def _structured_gmail_clarification(
    *,
    status: str,
    operation: str,
    clarification_request: str,
    message: str,
    source_label: str,
    query: str = "",
    reason_code: str = "",
    candidate_thread_ids: list[str] | None = None,
    missing_inputs: list[str] | None = None,
    suggested_next_steps: list[str] | None = None,
) -> dict[str, Any]:
    return GmailClarificationResult(
        status=status,
        clarification_request=clarification_request,
        operation=operation,
        reason_code=reason_code,
        message=message,
        source_label=source_label,
        query=query,
        candidate_count=len(candidate_thread_ids or []),
        candidate_thread_ids=candidate_thread_ids or [],
        missing_inputs=missing_inputs or [],
        suggested_next_steps=suggested_next_steps or [],
        send_enabled=False,
        draft_created=False,
        labels_modified=False,
    ).model_dump(mode="json")


def _gmail_target_block_payload(
    *,
    reason_code: str,
    human_summary: str,
    clarification_request: str,
    query: str,
    attempts: list[dict[str, object]],
    candidate_count: int = 0,
) -> dict[str, Any]:
    output = {
        "status": "blocked",
        "block_kind": reason_code,
        "summary": human_summary,
        "clarification_request": clarification_request,
        "candidate_count": candidate_count,
        "send_enabled": False,
        "draft_created": False,
        "labels_modified": False,
    }
    return {
        "status": "blocked",
        "block_kind": reason_code,
        "send_enabled": False,
        "human_summary": human_summary,
        "output_type": "GmailClarificationResult",
        "output": output,
        "retrieval_diagnostics": {
            "provider": "gmail",
            "operation": "resolve_message_for_draft_reply",
            "query": query,
            "attempts": attempts,
            "candidate_count": candidate_count,
            "provider_read": True,
            "provider_write": False,
        },
        "side_effects": {
            "gmail_draft_created": False,
            "gmail_draft_updated": False,
            "labels_modified": False,
            "email_sent": False,
            "send_enabled": False,
        },
    }


_GMAIL_RELAXED_SUBJECT_STOPWORDS = frozenset(
    {
        "about",
        "and",
        "for",
        "from",
        "kind",
        "new",
        "needs",
        "of",
        "the",
        "this",
        "why",
        "with",
    }
)


def _relaxed_gmail_subject_query(subject: str) -> str:
    tokens = [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{2,}", subject)
        if token.lower() not in _GMAIL_RELAXED_SUBJECT_STOPWORDS
    ]
    distinctive = list(dict.fromkeys(tokens))[:6]
    if len(distinctive) < 2:
        return ""
    return " ".join(f"subject:{token}" for token in distinctive)


_EVENT_TIMEZONE_OFFSETS = {
    "EDT": timezone(timedelta(hours=-4)),
    "EST": timezone(timedelta(hours=-5)),
    "PDT": timezone(timedelta(hours=-7)),
    "PST": timezone(timedelta(hours=-8)),
    "CDT": timezone(timedelta(hours=-5)),
    "CST": timezone(timedelta(hours=-6)),
    "MDT": timezone(timedelta(hours=-6)),
    "MST": timezone(timedelta(hours=-7)),
    "UTC": UTC,
}


def _request_event_start(
    request_text: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Resolve a natural event selector to an Eastern start instant."""

    source = " ".join(str(request_text or "").replace("’", "'").split())
    if not re.search(r"\b(?:interview|meeting|event|call|appointment)\b", source, re.I):
        return None
    time_match = re.search(
        r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
        r"(?P<meridiem>a\.?m\.?|p\.?m\.?)\b",
        source,
        re.I,
    )
    if time_match is None:
        return None
    eastern = ZoneInfo("America/New_York")
    local_now = now.astimezone(eastern) if now is not None else datetime.now(eastern)
    event_date: date | None = None
    if re.search(r"\btomorrow(?:'s)?\b", source, re.I):
        event_date = local_now.date() + timedelta(days=1)
    else:
        date_match = re.search(
            r"\b(?:mon|tue|wed|thu|fri|sat|sun)?\w*\s*"
            r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
            r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
            r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{4}))?",
            source,
            re.I,
        )
        if date_match is not None:
            year = int(date_match.group("year") or local_now.year)
            month = datetime.strptime(date_match.group("month")[:3], "%b").month
            event_date = date(year, month, int(date_match.group("day")))
    if event_date is None:
        return None
    hour = int(time_match.group("hour")) % 12
    if time_match.group("meridiem").lower().startswith("p"):
        hour += 12
    minute = int(time_match.group("minute") or 0)
    return datetime.combine(event_date, time(hour, minute), tzinfo=eastern)


def _subject_event_start(subject: str) -> datetime | None:
    """Parse a Calendar-style Gmail subject and normalize its start instant."""

    match = re.search(
        r"\b(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
        r"(?P<day>\d{1,2}),\s*(?P<year>\d{4})\s+"
        r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
        r"(?P<meridiem>am|pm)\s*-.*?\((?P<timezone>[A-Z]{3})\)",
        str(subject or ""),
        re.I,
    )
    if match is None:
        return None
    timezone_name = match.group("timezone").upper()
    source_timezone = _EVENT_TIMEZONE_OFFSETS.get(timezone_name)
    if source_timezone is None:
        return None
    hour = int(match.group("hour")) % 12
    if match.group("meridiem").lower() == "pm":
        hour += 12
    value = datetime(
        int(match.group("year")),
        datetime.strptime(match.group("month").title(), "%b").month,
        int(match.group("day")),
        hour,
        int(match.group("minute") or 0),
        tzinfo=source_timezone,
    )
    return value.astimezone(ZoneInfo("America/New_York"))


def _exact_schedule_thread_id(
    request_text: str,
    summaries: list[GmailThreadSummaryResult],
    *,
    now: datetime | None = None,
) -> str:
    """Select one non-canceled Gmail thread whose event start matches exactly."""

    requested_start = _request_event_start(request_text, now=now)
    if requested_start is None:
        return ""
    matches: list[str] = []
    for summary in summaries:
        subject = str(summary.subject or "")
        if re.search(r"\b(?:cancel(?:ed|led)|declined|deleted)\b", subject, re.I):
            continue
        candidate_start = _subject_event_start(subject)
        if candidate_start is None:
            continue
        if candidate_start == requested_start:
            matches.append(summary.thread_id)
    unique = list(dict.fromkeys(item for item in matches if item))
    return unique[0] if len(unique) == 1 else ""


def _resolve_live_sdk_message_refs(
    gmail: Any,
    *,
    request_text: str,
    label: str,
    query: str,
    max_results: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve one message with bounded read-only fallbacks and safe diagnostics."""

    bounded_max = max(5, min(10, int(max_results)))
    attempts: list[dict[str, object]] = []

    def search(search_label: str | None, search_query: str, scope: str) -> list[dict[str, Any]]:
        raw_refs = list(
            gmail.list_recent_messages(
                label=search_label,
                max_results=bounded_max,
                query=search_query,
            )
        )
        refs: list[dict[str, Any]] = []
        seen_threads: set[str] = set()
        for ref in raw_refs:
            thread_id = str(ref.get("threadId") or ref.get("id") or "").strip()
            if not thread_id or thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            refs.append(ref)
        attempts.append(
            {
                "scope": scope,
                "query": search_query,
                "candidate_count": len(refs),
            }
        )
        return refs

    refs = search(label, query, "requested_label")
    subject = extract_gmail_subject_hint(request_text)
    if not refs and subject and label:
        refs = search(None, query, "all_mail_exact_subject")
    relaxed_query = _relaxed_gmail_subject_query(subject)
    if not refs and relaxed_query and relaxed_query != query:
        refs = search(None, relaxed_query, "all_mail_relaxed_subject")
    if len(refs) > 1:
        # Search recall is not the same as target ambiguity. Read a small,
        # deduplicated candidate set and let the Gmail selection specialist
        # rank sanitized thread evidence against the complete current request.
        # Provider identity remains bound in Python and no write is possible in
        # this phase.
        thread_summaries: list[GmailThreadSummaryResult] = []
        for ref in refs:
            thread_id = str(ref.get("threadId") or ref.get("id") or "").strip()
            if not thread_id:
                continue
            thread_summaries.append(
                _thread_summary_result_from_payload(
                    thread=gmail.get_thread(thread_id),
                    source_label=label or "ALL_MAIL",
                    query=query,
                )
            )
        schedule_thread_id = _exact_schedule_thread_id(request_text, thread_summaries)
        if schedule_thread_id:
            selected_refs = [
                ref
                for ref in refs
                if str(ref.get("threadId") or ref.get("id") or "").strip()
                == schedule_thread_id
            ]
            if len(selected_refs) == 1:
                attempts.append(
                    {
                        "scope": "exact_schedule_match",
                        "query": query,
                        "candidate_count": len(refs),
                        "selected_count": 1,
                        "selection_reason": "timezone_normalized_event_start",
                    }
                )
                return selected_refs, {
                    "provider": "gmail",
                    "operation": "resolve_message_for_draft_reply",
                    "query": query,
                    "attempts": attempts,
                    "candidate_count": len(refs),
                    "selected_count": 1,
                    "selection_reason": "timezone_normalized_event_start",
                    "candidate_ranking_used": False,
                    "provider_read": True,
                    "provider_write": False,
                }
        ranking = rank_gmail_candidates_for_request(
            operator_request=request_text,
            summaries=thread_summaries,
            live_sdk=True,
        )
        ranked_items = sorted(
            (
                item
                for item in ranking.result.candidates
                if item.disposition == "candidate"
            ),
            key=lambda item: (item.needs_reply, item.relevance_score),
            reverse=True,
        )
        selected_thread_id = ""
        selection_reason = ""
        if len(ranking.ranked_thread_ids) == 1:
            selected_thread_id = ranking.ranked_thread_ids[0]
            selection_reason = "single_semantic_candidate"
        elif ranked_items:
            top = ranked_items[0]
            runner_up = ranked_items[1] if len(ranked_items) > 1 else None
            margin = top.relevance_score - (
                runner_up.relevance_score if runner_up is not None else 0.0
            )
            if top.needs_reply and top.relevance_score >= 0.85 and margin >= 0.20:
                message_to_thread = {
                    str(payload.get("id") or "").strip(): str(
                        payload.get("threadId") or ""
                    ).strip()
                    for payload in ranking.candidate_payloads
                }
                selected_thread_id = message_to_thread.get(top.message_id, "")
                selection_reason = "high_confidence_semantic_margin"
        if selected_thread_id:
            selected_refs = [
                ref
                for ref in refs
                if str(ref.get("threadId") or ref.get("id") or "").strip()
                == selected_thread_id
            ]
            if len(selected_refs) == 1:
                attempts.append(
                    {
                        "scope": "semantic_candidate_ranking",
                        "query": query,
                        "candidate_count": len(refs),
                        "selected_count": 1,
                        "selection_reason": selection_reason,
                    }
                )
                return selected_refs, {
                    "provider": "gmail",
                    "operation": "resolve_message_for_draft_reply",
                    "query": query,
                    "attempts": attempts,
                    "candidate_count": len(refs),
                    "selected_count": 1,
                    "selection_reason": selection_reason,
                    "candidate_ranking_used": True,
                    "provider_read": True,
                    "provider_write": False,
                }
        attempts.append(
            {
                "scope": "semantic_candidate_ranking",
                "query": query,
                "candidate_count": len(refs),
                "selected_count": 0,
                "selection_reason": "insufficient_unique_evidence",
            }
        )
        raise GmailTargetResolutionError(
            _gmail_target_block_payload(
                reason_code="gmail_target_ambiguous",
                human_summary=(
                    "I read and ranked the bounded Gmail matches, but more than one "
                    "conversation remained plausible, so I did not guess which thread to use."
                ),
                clarification_request=(
                    "Add the sender, meeting time, or exact subject to identify one thread."
                ),
                query=query,
                attempts=attempts,
                candidate_count=len(refs),
            )
        )
    if not refs:
        raise GmailTargetResolutionError(
            _gmail_target_block_payload(
                reason_code="gmail_target_not_found",
                human_summary=(
                    "I checked the connected Gmail mailbox using the requested scope, "
                    "All Mail with the exact subject, and a bounded relaxed-subject search, "
                    "but found no matching message. No mailbox data was changed."
                ),
                clarification_request=(
                    "Confirm the connected Gmail account, or provide another subject, "
                    "sender, or approximate date."
                ),
                query=query,
                attempts=attempts,
            )
        )
    return refs, {
        "provider": "gmail",
        "operation": "resolve_message_for_draft_reply",
        "query": query,
        "attempts": attempts,
        "candidate_count": 1,
        "provider_read": True,
        "provider_write": False,
    }


def _dedupe_thread_ids(refs: list[dict[str, Any]]) -> list[str]:
    thread_ids: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        thread_id = str(ref.get("threadId") or ref.get("id") or "").strip()
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        thread_ids.append(thread_id)
    return thread_ids


def _thread_summary_result_from_payload(
    *,
    thread: dict[str, Any],
    source_label: str,
    query: str,
) -> GmailThreadSummaryResult:
    messages = [
        GmailThreadSummaryMessage(
            message_id=str(item.get("id") or ""),
            received_at=str(item.get("received_at") or ""),
            sender_name=str(item.get("sender_name") or ""),
            sender_email=str(item.get("sender_email") or ""),
            subject=str(item.get("subject") or ""),
            snippet=str(item.get("snippet") or ""),
            prior_labels=[
                str(label) for label in item.get("prior_labels", []) if str(label).strip()
            ],
            summary=str(item.get("thread_summary") or item.get("snippet") or ""),
        )
        for item in thread.get("messages", [])
        if isinstance(item, dict)
    ]
    return GmailThreadSummaryResult(
        thread_id=str(thread.get("thread_id") or ""),
        source_label=source_label,
        query=query,
        subject=str(thread.get("subject") or ""),
        summary=str(thread.get("summary") or ""),
        thread_context=str(thread.get("thread_context") or ""),
        message_count=int(thread.get("message_count") or 0),
        latest_received_at=str(thread.get("latest_received_at") or ""),
        participants=[str(item) for item in thread.get("participants", []) if str(item).strip()],
        action_items=[str(item) for item in thread.get("action_items", []) if str(item).strip()],
        deadlines=[str(item) for item in thread.get("deadlines", []) if str(item).strip()],
        open_questions=[
            str(item) for item in thread.get("open_questions", []) if str(item).strip()
        ],
        triage_limitations=[
            str(item) for item in thread.get("triage_limitations", []) if str(item).strip()
        ],
        messages=messages,
        send_enabled=False,
        draft_created=False,
        labels_modified=False,
    )


def _render_thread_summary_markdown(payload: dict[str, Any]) -> str:
    threads = payload.get("threads", [])
    if not threads:
        return "No Gmail threads found."
    parts: list[str] = []
    for thread in threads:
        parts.append(str(thread.get("summary") or "Thread summary unavailable."))
        action_items = thread.get("action_items") or []
        deadlines = thread.get("deadlines") or []
        open_questions = thread.get("open_questions") or []
        if action_items:
            parts.append("Action items:")
            parts.extend(f"- {item}" for item in action_items)
        if deadlines:
            parts.append("Deadlines:")
            parts.extend(f"- {item}" for item in deadlines)
        if open_questions:
            parts.append("Open questions:")
            parts.extend(f"- {item}" for item in open_questions)
    return "\n".join(parts)


def _render_clarification_markdown(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("message") or "Clarification required.")]
    clarification_request = str(payload.get("clarification_request") or "")
    if clarification_request:
        lines.extend(["", clarification_request])
    missing_inputs = payload.get("missing_inputs") or []
    if missing_inputs:
        lines.append("")
        lines.append("Missing inputs:")
        lines.extend(f"- {item}" for item in missing_inputs)
    suggested_next_steps = payload.get("suggested_next_steps") or []
    if suggested_next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(f"- {item}" for item in suggested_next_steps)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    dry_run_default = cli_default_dry_run()
    live_gmail_default = cli_default_live_gmail()
    parser = argparse.ArgumentParser(description="Run the Gmail triage agent.")
    parser.add_argument("--fixture", default=None, help="Path to a local plain-text email fixture.")
    parser.add_argument(
        "--fixtures",
        nargs="+",
        default=None,
        help="Multiple local plain-text email fixtures for priority grouping.",
    )
    parser.add_argument(
        "--subject", default="", help="Subject override or subject for inline input."
    )
    parser.add_argument(
        "--request",
        default="",
        help="Optional operator request to include in SDK synthesis context.",
    )
    parser.add_argument("--sender-name", default="", help="Sender display name.")
    parser.add_argument("--sender-email", default="", help="Sender email address.")
    parser.add_argument(
        "--email-style-profile-fixture",
        default=None,
        help="Optional approved local aggregate email style profile fixture.",
    )
    parser.add_argument(
        "--email-style-profile-id",
        default=None,
        help="Optional approved aggregate email style profile id from local SQLite.",
    )
    parser.add_argument(
        "--founder-fit-profile",
        default=None,
        help=(
            "Optional approved founder-fit JSON profile. Used for business-fit "
            "and reply drafting only when approved_for_drafting=true."
        ),
    )
    parser.add_argument(
        "--live-gmail",
        action=argparse.BooleanOptionalAction,
        default=live_gmail_default,
        help="Use live Gmail API access.",
    )
    parser.add_argument(
        "--label-filter",
        default=None,
        help="Required Gmail label id/name for live Gmail unless --allow-inbox is passed.",
    )
    parser.add_argument(
        "--gmail-query",
        "--query",
        dest="gmail_query",
        default=None,
        help="Optional Gmail search query to narrow live read-only triage within the label scope.",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="Optional Gmail thread id to summarize directly in read-only mode.",
    )
    parser.add_argument(
        "--thread-summary",
        action="store_true",
        default=False,
        help=(
            "Read and summarize one or more Gmail threads instead of per-message triage. "
            "This mode is always read-only."
        ),
    )
    parser.add_argument(
        "--message-count",
        action="store_true",
        default=False,
        help="Count a bounded Gmail result set with read-only pagination.",
    )
    parser.add_argument(
        "--message-projection",
        action="store_true",
        default=False,
        help="Return selected metadata fields for one bounded Gmail result set.",
    )
    parser.add_argument(
        "--contact-lookup",
        action="store_true",
        default=False,
        help=(
            "Answer one known-contact question from a bounded Gmail search using "
            "the read-only Agents SDK specialist path."
        ),
    )
    parser.add_argument(
        "--requested-field",
        action="append",
        choices=["subject", "sender", "date", "snippet"],
        default=[],
    )
    parser.add_argument("--expected-result-count", type=int, default=None)
    parser.add_argument(
        "--mailbox-direction",
        choices=["unspecified", "inbound", "outbound", "any"],
        default="unspecified",
    )
    parser.add_argument(
        "--date-scope",
        choices=["unspecified", "today", "yesterday", "specific_date", "rolling_window"],
        default="unspecified",
    )
    parser.add_argument("--provider-timezone", default="America/New_York")
    parser.add_argument("--window-start", default="")
    parser.add_argument("--window-end", default="")
    parser.add_argument(
        "--max-messages", type=int, default=1, help="Maximum Gmail messages to process."
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=3,
        help="Unread Gmail lookback window for priority grouping.",
    )
    parser.add_argument(
        "--priority-grouping",
        action="store_true",
        default=False,
        help="Run LLM SDK batch priority grouping for unread Gmail messages.",
    )
    parser.add_argument(
        "--test-pack-report-dir",
        default=None,
        help=(
            "Write sanitized GT-1 test-pack markdown and JSON report artifacts to this "
            "local directory. Only valid with --priority-grouping SDK synthesis."
        ),
    )
    parser.add_argument(
        "--create-draft",
        action="store_true",
        default=False,
        help="Create Gmail drafts for reply-needed messages.",
    )
    parser.add_argument(
        "--update-draft",
        action="store_true",
        default=False,
        help="Resolve and update one existing Gmail draft without sending it.",
    )
    parser.add_argument("--draft-subject-hint", default="")
    parser.add_argument("--draft-recipient-hint", default="")
    parser.add_argument(
        "--approval-reference",
        default="",
        help=(
            "Exact scoped operator approval reference for a requested Gmail draft write. "
            "This never authorizes sending."
        ),
    )
    parser.add_argument(
        "--expected-account",
        default="",
        help="Expected authenticated Gmail account for a scoped draft write.",
    )
    parser.add_argument(
        "--apply-labels",
        action="store_true",
        default=False,
        help="Apply recommended Keystone labels to live Gmail messages.",
    )
    parser.add_argument(
        "--preview-labels",
        action="store_true",
        default=False,
        help="Preview Gmail label add/remove operations without applying labels.",
    )
    parser.add_argument(
        "--cleanup-labels",
        action="store_true",
        default=False,
        help="Remove obsolete Keystone-managed Gmail labels. Preview first with --preview-labels.",
    )
    parser.add_argument(
        "--allow-inbox",
        action=argparse.BooleanOptionalAction,
        default=live_gmail_default,
        help="Allow live Gmail to read INBOX when no label filter is supplied.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=dry_run_default,
        help=(
            "Use deterministic fixture mode. Defaults to KEYSTONE_DRY_RUN or true. "
            "Pass --no-dry-run for live flags."
        ),
    )
    parser.add_argument(
        "--sdk", action="store_true", help="Use explicit SDK mode instead of fixture mode."
    )
    add_sdk_run_arguments(parser)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    parser.add_argument("--markdown", action="store_true", help="Print markdown output.")
    add_orchestrator_review_arguments(parser)
    parser.add_argument(
        "--save", action="store_true", help="Save triage and audit records to SQLite."
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument(
        "--request-approval", action="store_true", help="Post a draft approval request."
    )
    parser.add_argument(
        "--ask-feedback",
        action="store_true",
        help="Attach an optional structured operator feedback request to the review item.",
    )
    parser.add_argument(
        "--approval-decision",
        choices=APPROVAL_DECISIONS,
        default="pending",
        help="Manual approval decision to apply to this fixture run.",
    )
    parser.add_argument(
        "--approval-scope",
        choices=APPROVAL_SCOPES,
        default=ApprovalScope.SEND.value,
        help="Workflow scope the approval decision applies to.",
    )
    parser.add_argument("--reviewer", default="", help="Human reviewer for saved approvals.")
    parser.add_argument("--approval-notes", default="", help="Reviewer notes for saved approvals.")
    parser.add_argument(
        "--live-slack", action="store_true", help="Post approval notification to Slack."
    )
    return parser


def _apply_live_test_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Promote env-backed live test defaults for SDK-only Gmail paths."""

    if (
        (args.priority_grouping or args.contact_lookup)
        and not args.sdk
        and not sdk_execution_requested(args)
        and cli_default_live_sdk()
    ):
        args.live_sdk = True
    return args


def _style_profile_context(value: Any | None) -> str:
    if value is None or not getattr(value, "approved_for_drafting", False):
        return ""
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)


def _orchestrator_review_request_summary(
    args: argparse.Namespace,
    *,
    fallback: str,
) -> str:
    request = str(getattr(args, "request", "") or "").strip()
    if request:
        return request
    preflight = getattr(args, "orchestrator_preflight", None)
    if isinstance(preflight, dict):
        request_text = str(preflight.get("request_text") or "").strip()
        if request_text:
            return request_text
        memo = preflight.get("preflight_memo")
        if isinstance(memo, dict):
            raw_request = str(memo.get("raw_request") or "").strip()
            if raw_request:
                return raw_request
    plan = getattr(args, "manual_request_plan", None)
    if isinstance(plan, dict):
        objective = str(plan.get("objective") or "").strip()
        if objective:
            return objective
    return fallback


def _load_requested_style_profile(args: argparse.Namespace) -> Any | None:
    if args.email_style_profile_id and args.email_style_profile_fixture:
        raise SystemExit(
            "Use either --email-style-profile-id or --email-style-profile-fixture, not both."
        )
    if args.email_style_profile_id:
        profile = load_email_style_profile_from_storage(
            args.email_style_profile_id,
            database_url=args.database_url,
        )
        if profile is None:
            raise SystemExit(
                "No approved email style profile found in local SQLite for "
                f"'{args.email_style_profile_id}'. Generated profiles must be "
                "approved_for_drafting before use."
            )
        return profile
    if args.email_style_profile_fixture:
        return load_email_style_profile_fixture(args.email_style_profile_fixture)
    return None


def _run_sdk_synthesis(
    args: argparse.Namespace,
    *,
    email_style_profile: Any | None,
    founder_fit_profile: Any | None,
) -> dict[str, Any]:
    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
            "apply_labels": "--apply-labels",
            "preview_labels": "--preview-labels",
            "cleanup_labels": "--cleanup-labels",
        },
    )
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="read-only Gmail API calls for SDK synthesis",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    if args.create_draft and args.update_draft:
        raise SystemExit("Use either --create-draft or --update-draft, not both.")
    if args.create_draft or args.update_draft:
        if not args.live_gmail or not live:
            raise SystemExit(
                "Gmail draft writes with SDK synthesis require --live-gmail and --live-sdk."
            )
        if not str(args.approval_reference or "").strip():
            raise SystemExit(
                "Gmail draft writes require an exact --approval-reference from the "
                "authenticated operator command."
            )
        if not str(args.expected_account or "").strip():
            raise SystemExit("Gmail draft writes require --expected-account for scoping.")
    if args.update_draft and not (
        str(args.draft_subject_hint or "").strip() or str(args.draft_recipient_hint or "").strip()
    ):
        raise SystemExit(
            "--update-draft requires a natural subject or recipient hint; no draft was changed."
        )
    style_context = _style_profile_context(email_style_profile)
    founder_context = founder_drafting_context(founder_fit_profile)
    preflight_context = orchestrator_preflight_context_text(args)
    if args.live_gmail and not args.update_draft:
        return _run_agent_owned_live_gmail_synthesis(
            args,
            run_config=run_config,
            live=live,
            style_context=style_context,
            founder_context=founder_context,
            preflight_context=preflight_context,
        )
    retrieval_diagnostics: dict[str, Any] = {}

    resolved_draft: dict[str, Any] = {}

    def retrieve() -> GmailMessageEnvelope:
        if args.live_gmail:
            if args.update_draft:
                gmail = GmailTool(live=True)
                resolution = resolve_unique_gmail_draft(
                    gmail,
                    subject_hint=args.draft_subject_hint,
                    recipient_hint=args.draft_recipient_hint,
                )
                if resolution.get("status") != "resolved":
                    raise RuntimeError(
                        "Gmail draft target was not unique; no draft was changed. "
                        f"reason_code={resolution.get('reason_code')} "
                        f"candidate_count={resolution.get('candidate_count')}"
                    )
                draft = gmail.get_draft(str(resolution.get("draft_id") or ""))
                resolved_draft.update(draft)
                resolved_draft["resolution"] = resolution
                return GmailMessageEnvelope(
                    message_id=str(draft.get("message_id") or ""),
                    thread_id="",
                    sender_name="Draft recipient",
                    sender_email=str(draft.get("to") or ""),
                    subject=str(draft.get("subject") or ""),
                    normalized_body=str(draft.get("body") or ""),
                    triage_limitations=[
                        "Existing Gmail draft resolved uniquely by natural subject/recipient "
                        "reference; provider ID remains internal."
                    ],
                )
            label = _priority_grouping_source_label(args)
            gmail = GmailTool(live=True)
            message_refs, resolution_diagnostics = _resolve_live_sdk_message_refs(
                gmail,
                request_text=str(args.request or ""),
                label=label,
                query=args.gmail_query,
                max_results=args.max_messages,
            )
            retrieval_diagnostics.update(resolution_diagnostics)
            if len(message_refs) != 1:
                raise RuntimeError(
                    "Live Gmail SDK synthesis requires exactly one selected message. "
                    "Narrow --gmail-query or use --max-messages 1."
                )
            message = gmail.get_message(str(message_refs[0]["id"]))
            if not message.get("id"):
                message["id"] = str(message_refs[0]["id"])
            return _thread_enriched_live_envelope(gmail, message)
        fixture = load_email_fixture(
            args.fixture,
            subject=args.subject,
            sender_name=args.sender_name,
            sender_email=args.sender_email,
        )
        return email_fixture_to_envelope(fixture)

    def normalize(envelope: GmailMessageEnvelope) -> GmailTriageSDKInput:
        typed_input = GmailTriageSDKInput.from_envelope(envelope)
        typed_input = replace(
            typed_input, request=str(args.request or ""), advisory_context=preflight_context,
        )
        if style_context or founder_context:
            return replace(
                typed_input,
                email_style_profile=style_context,
                founder_fit_context=founder_context,
            )
        return typed_input

    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(
            include_tools=False,
            request_text=args.request,
            compact_instructions=args.compact_instructions,
        ),
        output_type=EmailTriageResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary=args.request or args.subject or args.fixture or "gmail triage SDK synthesis",
        input_audit_payload={
            "fixture": args.fixture,
            "subject": args.subject,
            "sender_email": args.sender_email,
            "request": args.request,
            "live_gmail": bool(args.live_gmail),
            "gmail_query": args.gmail_query,
            "label_filter": args.label_filter,
            "allow_inbox": bool(args.allow_inbox),
            "email_style_profile_fixture": args.email_style_profile_fixture,
            "email_style_profile_id": args.email_style_profile_id,
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_fit_profile,
            ),
            "sdk_synthesis": True,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    if retrieval_diagnostics:
        payload["retrieval_diagnostics"] = retrieval_diagnostics
    _repair_gmail_triage_output_hygiene(payload)
    if args.create_draft:
        draft_result = _create_verified_sdk_reply_draft(args, outcome)
        payload["gmail_draft_result"] = draft_result
        payload["side_effects"] = {
            "gmail_draft_created": bool(draft_result.get("verification", {}).get("passed")),
            "gmail_draft_updated": False,
            "email_sent": False,
            "send_enabled": False,
            "approval_reference": str(args.approval_reference),
        }
    elif args.update_draft:
        draft_result = _update_verified_sdk_draft(args, outcome, resolved_draft)
        payload["gmail_draft_result"] = draft_result
        payload["side_effects"] = {
            "gmail_draft_created": False,
            "gmail_draft_updated": bool(draft_result.get("verification", {}).get("passed")),
            "email_sent": False,
            "send_enabled": False,
            "approval_reference": str(args.approval_reference),
        }
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=outcome.final_output,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=args.subject or args.fixture or "gmail triage SDK synthesis",
            ),
            run_type="live SDK" if live else "local SDK",
        )
    attach_orchestrator_preflight_payload(payload, args)
    return payload


def _run_agent_owned_live_gmail_synthesis(
    args: argparse.Namespace,
    *,
    run_config: Any | None,
    live: bool,
    style_context: str,
    founder_context: str,
    preflight_context: str,
) -> dict[str, Any]:
    """Let Gmail Triage query, read, rank, and decide inside one SDK loop."""

    continuation_message_id, continuation_thread_id = _verified_gmail_continuation_identity(
        load_specialist_execution_context_from_env()
    )
    typed_input = GmailTriageSDKInput(
        subject="",
        body="",
        request=str(args.request or "").strip(),
        advisory_context=preflight_context,
        message_id=continuation_message_id,
        thread_id=continuation_thread_id,
        email_style_profile=style_context,
        founder_fit_context=founder_context,
        gmail_query_hint=(
            ""
            if continuation_message_id or continuation_thread_id
            else str(args.gmail_query or "").strip()
        ),
        triage_limitations=[
            "No message was preselected by Python; Gmail Triage owns bounded query, "
            "candidate reading, selection, reply relevance, and draft wording."
        ],
    )
    started_at = time_module.time()
    selected_contexts = []
    result = run_gmail_triage_sdk(
        typed_input,
        run_config=run_config,
        live=live,
        attach_tools=True,
        compact_instructions=args.compact_instructions,
        manual_request_plan=getattr(args, "manual_request_plan", None),
        provider_selection_required=not bool(
            continuation_message_id or continuation_thread_id
        ),
        provider_context_read_required=bool(
            continuation_message_id or continuation_thread_id
        ),
        repair_invalid_selection=True,
        selected_context_callback=selected_contexts.append,
    )
    if run_config is not None:
        model_provider = "local"
        model_name = str(getattr(run_config, "model", "") or "sdk-local")
        model_run_mode = "local_sdk"
    else:
        model_config = get_runtime_agent_model_config("gmail_triage")
        model_provider = model_config.provider
        model_name = model_config.model
        model_run_mode = "live_sdk" if live else "sdk"
    outcome = SDKSynthesisOutcome(
        agent_name="gmail_triage",
        raw_context={
            "mode": "agent_owned_provider_selection",
            "preacquired_provider_context": False,
        },
        typed_input=typed_input,
        result=result,
        storage={},
        audit_notes=(
            (
                "Gmail Triage returned no selected conversation and requested more context."
                if result.final_output.decision.needs_more_context else
                "Gmail Triage called bounded Gmail tools and selected the conversation."
            ),
            "Python validated the decision against the actual query/read evidence.",
            "No deterministic helper selected or substituted a Gmail candidate.",
            "No email was sent.",
        ),
        model_provider=model_provider,
        model_name=model_name,
        model_run_mode=model_run_mode,
        usage=result.usage,
        cost=result.cost,
        budget_guard=result.budget_guard,
        request_cache=result.request_cache,
        execution_telemetry=result.execution_telemetry,
        started_at_unix=started_at,
        ended_at_unix=time_module.time(),
    )
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    decision_telemetry = result.request_cache.get("decision_ownership")
    if isinstance(decision_telemetry, dict):
        payload["decision_ownership"] = decision_telemetry
    tool_execution = result.request_cache.get("tool_execution")
    if isinstance(tool_execution, dict):
        payload["tool_execution"] = tool_execution
    payload["tool_receipts"] = list(result.tool_receipts)
    _repair_gmail_triage_output_hygiene(payload)
    if not result.final_output.decision.needs_more_context:
        payload["human_summary"] = render_gmail_selected_answer(
            result.final_output,
            selected_context=selected_contexts[-1] if selected_contexts else None,
        )
    if result.final_output.decision.needs_more_context:
        evidence = decision_telemetry if isinstance(decision_telemetry, dict) else {}
        no_matches = (
            evidence.get("candidate_count") == 0
            and evidence.get("query_provider_read_performed") is True
            and int(evidence.get("query_output_count") or 0) > 0
        )
        payload.update(
            {
                "status": "needs_input",
                "block_kind": ("gmail_search_no_matches" if no_matches else
                               "gmail_agent_requested_more_context"),
                "human_summary": (
                    "Gmail returned no messages for the bounded searches performed; "
                    "the requested email has not been read. Review the query filters "
                    "against the clues already supplied before retrying. "
                    "No Gmail data was changed."
                    if no_matches else
                    "Gmail could not select the requested conversation from the available "
                    "evidence without guessing. No Gmail data was changed."
                ),
            }
        )
    if args.create_draft and not result.final_output.decision.needs_more_context:
        draft_result = _create_verified_sdk_reply_draft(args, outcome)
        payload["gmail_draft_result"] = draft_result
        payload["side_effects"] = {
            "gmail_draft_created": bool(draft_result.get("verification", {}).get("passed")),
            "gmail_draft_updated": False,
            "email_sent": False,
            "send_enabled": False,
            "approval_reference": str(args.approval_reference),
        }
    elif args.create_draft:
        payload["gmail_draft_result"] = {
            "status": "blocked",
            "reason_code": "gmail_selection_needs_input",
            "verification": {"passed": False},
            "sent": False,
            "send_enabled": False,
        }
        payload["side_effects"] = {
            "gmail_draft_created": False,
            "gmail_draft_updated": False,
            "email_sent": False,
            "send_enabled": False,
            "approval_reference": str(args.approval_reference),
        }
    if args.orchestrator_review:
        payload["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=result.final_output,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback="gmail agent-owned triage",
            ),
            run_type="live SDK" if live else "local SDK",
        )
    attach_orchestrator_preflight_payload(payload, args)
    attach_execution_public_result(payload)
    if args.save:
        payload["storage"] = {
            "agent_run": _save_agent_owned_gmail_run(
                args,
                payload=payload,
                model_provider=model_provider,
                model_name=model_name,
                live=live,
            )
        }
    return payload


def _save_agent_owned_gmail_run(
    args: argparse.Namespace,
    *,
    payload: Mapping[str, Any],
    model_provider: str,
    model_name: str,
    live: bool,
) -> dict[str, Any]:
    """Persist the canonical terminal result after decision validation and rendering."""

    public_result = payload.get("public_result")
    public_mapping = public_result if isinstance(public_result, Mapping) else {}
    status = str(public_mapping.get("status") or payload.get("status") or "blocked").strip()
    error = str(public_mapping.get("failure_summary") or "").strip() or None
    stored_output = {str(key): value for key, value in payload.items() if key != "storage"}
    return StorageTool(args.database_url).save_agent_run(
        agent_name="gmail_triage",
        input_payload={
            "request": str(args.request or ""),
            "live_gmail": True,
            "gmail_query_hint": str(args.gmail_query or ""),
            "agent_owned_selection": True,
        },
        input_summary=str(args.request or "gmail agent-owned triage")[:500],
        output=stored_output,
        model=(f"{model_provider}:{model_name}" if model_provider else model_name),
        dry_run=not live,
        status=status,
        error=error,
    )


def _verified_gmail_continuation_identity(
    execution_context: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Return one exact active Gmail identity from bounded continuation context."""

    if not isinstance(execution_context, Mapping):
        return "", ""
    values = execution_context.get("verified_provider_objects")
    if not isinstance(values, list | tuple):
        return "", ""
    candidates: list[tuple[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        if (
            str(value.get("provider_system") or "") != "gmail"
            or str(value.get("verification_status") or "") != "verified"
            or str(value.get("lifecycle_state") or "") != "active"
        ):
            continue
        object_type = str(value.get("object_type") or "")
        object_id = str(value.get("object_id") or "").strip()
        scope = value.get("provider_scope")
        scope = scope if isinstance(scope, Mapping) else {}
        message_id = str(scope.get("message_id") or "").strip()
        thread_id = str(scope.get("thread_id") or "").strip()
        if object_type == "gmail_message":
            message_id = object_id or message_id
        elif object_type == "gmail_thread":
            thread_id = object_id or thread_id
        elif object_type == "gmail_draft":
            continue
        if message_id or thread_id:
            candidates.append((message_id, thread_id))
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else ("", "")


def _create_verified_sdk_reply_draft(
    args: argparse.Namespace,
    outcome: Any,
) -> dict[str, Any]:
    envelope = outcome.raw_context
    if not isinstance(envelope, GmailMessageEnvelope):
        selected_message_id = str(outcome.final_output.message_id or "").strip()
        selected_thread_id = str(outcome.final_output.thread_id or "").strip()
        if not selected_message_id:
            raise SystemExit("Gmail draft creation requires one agent-selected message.")
        message = GmailTool(live=True).get_message(selected_message_id)
        if not message.get("id"):
            message["id"] = selected_message_id
        envelope = _thread_enriched_live_envelope(GmailTool(live=True), message)
        if selected_thread_id and envelope.thread_id != selected_thread_id:
            raise SystemExit(
                "The selected Gmail message no longer belongs to the agent-selected thread; "
                "no draft was created."
            )
    draft_reply = str(outcome.final_output.draft_reply or "").strip()
    if not draft_reply:
        raise SystemExit(
            "Gmail Triage did not recommend a reply, so no provider draft was created."
        )
    reply_subject = envelope.subject.strip()
    if not reply_subject.lower().startswith("re:"):
        reply_subject = f"Re: {reply_subject}".strip()
    return execute_approved_gmail_draft_reply_action(
        GmailTool(live=True),
        message_id=envelope.message_id,
        body=draft_reply,
        expected_to=envelope.sender_email,
        expected_subject=reply_subject,
        expected_account=str(args.expected_account),
        approval_reference=str(args.approval_reference),
    )


def _update_verified_sdk_draft(
    args: argparse.Namespace,
    outcome: Any,
    resolved_draft: dict[str, Any],
) -> dict[str, Any]:
    draft_reply = str(outcome.final_output.draft_reply or "").strip()
    if not draft_reply:
        raise SystemExit(
            "Gmail Triage returned no revised draft text, so no provider draft was changed."
        )
    draft_id = str(resolved_draft.get("draft_id") or "").strip()
    if not draft_id:
        raise SystemExit("No uniquely resolved Gmail draft ID was available for update.")
    return execute_approved_gmail_draft_action(
        GmailTool(live=True),
        to=str(resolved_draft.get("to") or ""),
        subject=str(resolved_draft.get("subject") or ""),
        body=draft_reply,
        expected_account=str(args.expected_account),
        approval_reference=str(args.approval_reference),
        draft_id=draft_id,
    )


def _repair_gmail_triage_output_hygiene(payload: dict[str, Any]) -> dict[str, Any]:
    """Repair narrow mixed-script glitches in human-facing Gmail triage fields."""

    if str(payload.get("output_type") or "").strip() != "EmailTriageResult":
        return payload
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    if not output:
        return payload
    repaired_fields: list[str] = []
    request_text = str(payload.get("input") or "")
    extracted_subject = _extract_inline_subject(request_text)
    current_subject = str(output.get("subject") or "").strip()
    if extracted_subject and _is_generic_gmail_subject(current_subject):
        output["subject"] = extracted_subject
        repaired_fields.append("subject")
    recommended_action = str(output.get("recommended_action") or "").strip()
    if _has_mixed_latin_non_latin_token(recommended_action):
        output["recommended_action"] = _fallback_recommended_action(output)
        repaired_fields.append("recommended_action")
        limitations = [
            str(item).strip()
            for item in output.get("triage_limitations") or []
            if str(item).strip()
        ]
        limitations.append(
            "Generated recommendation text contained mixed-script noise and was replaced with a conservative English fallback."
        )
        output["triage_limitations"] = list(dict.fromkeys(limitations))
    if repaired_fields:
        payload["output_hygiene"] = {
            "schema": "keystone.gmail_triage.output_hygiene.v1",
            "repair_applied": True,
            "repair_reason": ",".join(repaired_fields),
            "repaired_fields": repaired_fields,
        }
    return payload


def _extract_inline_subject(value: str) -> str:
    match = re.search(
        r"\bSubject:\s*(.+?)(?=\s+\b(?:From|Body):|\n|$)",
        str(value or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return " ".join(match.group(1).split())[:200]


def _is_generic_gmail_subject(value: str) -> bool:
    normalized = " ".join(str(value or "").lower().split())
    return normalized in {
        "",
        "gmail triage result",
        "manual gmail triage request",
        "manual triage request",
    }


def _has_mixed_latin_non_latin_token(value: str) -> bool:
    for token in re.findall(r"\S+", str(value or "")):
        has_latin = False
        has_non_latin = False
        for char in token:
            if not char.isalpha():
                continue
            name = unicodedata.name(char, "")
            if "LATIN" in name:
                has_latin = True
            else:
                has_non_latin = True
        if has_latin and has_non_latin:
            return True
    return False


def _fallback_recommended_action(output: dict[str, Any]) -> str:
    needs_reply = bool(output.get("needs_reply"))
    category = str(output.get("category") or "").strip()
    if needs_reply and category in {"collaboration_opportunity", "consulting_opportunity"}:
        return (
            "Reply with a brief note confirming interest, asking for scope and timing, "
            "and suggesting a short call or written summary review if helpful."
        )
    if needs_reply:
        return (
            "Review the message and prepare a concise reply for human approval before "
            "taking any external action."
        )
    return (
        "No reply appears required from the sanitized context; review manually if context changes."
    )


def _priority_grouping_source_label(args: argparse.Namespace) -> str:
    if args.label_filter:
        return str(args.label_filter)
    if args.allow_inbox:
        return "INBOX"
    return "UNREAD"


def _fixture_envelopes_for_priority_grouping(
    args: argparse.Namespace,
) -> list[GmailMessageEnvelope]:
    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    if not fixture_paths:
        raise SystemExit(
            "--priority-grouping requires --fixtures or --fixture unless --live-gmail is set."
        )

    envelopes: list[GmailMessageEnvelope] = []
    for index, fixture_path in enumerate(fixture_paths, start=1):
        fixture = load_email_fixture(
            fixture_path,
            subject=args.subject,
            sender_name=args.sender_name,
            sender_email=args.sender_email,
        )
        path = Path(str(fixture_path))
        fixture_key = path.stem or f"message-{index}"
        envelope = email_fixture_to_envelope(fixture).model_copy(
            update={
                "message_id": f"fixture-{index}-{fixture_key}",
                "thread_id": f"fixture-thread-{index}-{fixture_key}",
                "triage_limitations": [
                    "Fixture mode uses sanitized local email content; no live Gmail "
                    "reads were made."
                ],
            }
        )
        envelopes.append(envelope)
    return envelopes


def _envelope_from_live_message_payload(message: dict[str, Any]) -> GmailMessageEnvelope:
    sender_name, sender_email = parseaddr(str(message.get("from") or ""))
    return GmailMessageEnvelope.model_validate(
        message.get("envelope")
        or {
            "message_id": str(message.get("id") or ""),
            "thread_id": str(message.get("threadId") or ""),
            "received_at": str(message.get("received_at") or ""),
            "subject": str(message.get("subject") or ""),
            "sender_name": sender_name,
            "sender_email": sender_email,
            "snippet": str(message.get("snippet") or ""),
            "normalized_body": str(
                message.get("normalized_body")
                or message.get("body")
                or message.get("snippet")
                or ""
            ),
            "prior_labels": [
                str(label) for label in message.get("labelIds", []) if str(label).strip()
            ],
            "extracted_links": message.get("extracted_links", []),
            "attachment_metadata": message.get("attachment_metadata", []),
            "thread_summary": str(message.get("thread_summary") or ""),
            "thread_context": str(message.get("thread_context") or ""),
            "suspicious_signals": message.get("suspicious_signals", []),
            "triage_limitations": message.get("triage_limitations", []),
        }
    )


def _thread_enriched_live_envelope(
    gmail: Any,
    message: dict[str, Any],
) -> GmailMessageEnvelope:
    """Prefer full read-only thread context when the Gmail adapter exposes it."""

    envelope = _envelope_from_live_message_payload(message)
    thread_id = envelope.thread_id or str(message.get("threadId") or "")
    get_thread = getattr(gmail, "get_thread", None)
    if not thread_id or not callable(get_thread):
        return envelope

    thread = get_thread(thread_id)
    thread_messages = thread.get("messages") if isinstance(thread, dict) else []
    for thread_message in thread_messages or []:
        if not isinstance(thread_message, dict):
            continue
        thread_message_id = str(thread_message.get("message_id") or thread_message.get("id") or "")
        if thread_message_id != envelope.message_id:
            continue
        return _envelope_from_live_message_payload(thread_message)

    thread_context = str(thread.get("thread_context") or "") if isinstance(thread, dict) else ""
    thread_summary = str(thread.get("summary") or "") if isinstance(thread, dict) else ""
    message_count = int(thread.get("message_count") or 0) if isinstance(thread, dict) else 0
    if not (thread_context or thread_summary or message_count):
        return envelope
    limitations = list(envelope.triage_limitations)
    limitations.append("Full Gmail thread was read in sanitized read-only mode.")
    return envelope.model_copy(
        update={
            "thread_context": thread_context or envelope.thread_context,
            "thread_summary": envelope.thread_summary or thread_summary,
            "thread_message_count": message_count or envelope.thread_message_count,
            "triage_limitations": list(dict.fromkeys(limitations)),
        }
    )


_GMAIL_PRIORITY_ACTION_TERMS = (
    "action required",
    "approval",
    "certificate",
    "coi",
    "contract",
    "deadline",
    "due",
    "follow up",
    "fw:",
    "insurance",
    "introduction",
    "invoice",
    "keystone",
    "login",
    "meeting",
    "onboarding",
    "password",
    "payment",
    "please",
    "quote",
    "re:",
    "reply",
    "request",
    "review",
    "scope",
    "sow",
    "urgent",
)
_GMAIL_PRIORITY_NOISE_TERMS = (
    "ads setting",
    "digest",
    "newsletter",
    "roundup",
    "unsubscribe",
    "weekly",
)
_GMAIL_THREAD_CONTEXT_TERMS = (
    "certificate",
    "coi",
    "contract",
    "follow up",
    "fw:",
    "insurance",
    "introduction",
    "onboarding",
    "quote",
    "re:",
    "scope",
    "sow",
)


def _summary_text(summary: dict[str, Any]) -> str:
    return " ".join(
        str(summary.get(key) or "")
        for key in ("from", "subject", "snippet", "thread_summary", "thread_context")
    ).lower()


def _priority_summary_needs_full_message(summary: dict[str, Any]) -> bool:
    text = _summary_text(summary)
    if any(term in text for term in _GMAIL_PRIORITY_ACTION_TERMS):
        return True
    if any(term in text for term in _GMAIL_PRIORITY_NOISE_TERMS):
        return False
    return bool(str(summary.get("threadId") or "").strip())


def _priority_summary_needs_thread(summary: dict[str, Any]) -> bool:
    text = _summary_text(summary)
    return any(term in text for term in _GMAIL_THREAD_CONTEXT_TERMS)


def _summary_has_classification_context(summary: dict[str, Any]) -> bool:
    return any(str(summary.get(key) or "").strip() for key in ("from", "subject", "snippet"))


def _envelope_from_live_summary(summary: dict[str, Any]) -> GmailMessageEnvelope:
    envelope = _envelope_from_live_message_payload(
        {
            **summary,
            "body": str(summary.get("snippet") or summary.get("thread_summary") or ""),
            "normalized_body": str(summary.get("snippet") or summary.get("thread_summary") or ""),
            "labelIds": summary.get("labelIds") or summary.get("prior_labels") or [],
            "triage_limitations": [
                *[str(item) for item in summary.get("triage_limitations", []) if str(item).strip()],
                "GT-1 staged retrieval classified this item from Gmail search summary only.",
            ],
        }
    )
    return envelope


def _search_live_message_summaries(
    gmail: Any,
    *,
    label: str,
    max_results: int,
    query: str,
) -> list[dict[str, Any]]:
    search_summaries = getattr(gmail, "search_message_summaries", None)
    if callable(search_summaries):
        try:
            return list(search_summaries(label=label, max_results=max_results, query=query))
        except ToolGuardrailViolation:
            # A credential/reset message in one search page must not discard the
            # other safe candidates. Fall back to ID-only search; the staged
            # batch read below enforces guardrails per message and skips only
            # rejected items.
            pass
    return list(gmail.list_recent_messages(label=label, max_results=max_results, query=query))


def _batch_read_live_messages(
    gmail: Any,
    message_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not message_ids:
        return {}
    batch_get = getattr(gmail, "batch_get_messages", None)
    if callable(batch_get):
        messages = batch_get(message_ids, skip_blocked=True)
    else:
        messages = []
        for message_id in message_ids:
            try:
                messages.append(gmail.get_message(message_id))
            except ToolGuardrailViolation:
                continue
    return {
        str(message.get("id") or message.get("message_id") or ""): message
        for message in messages
        if str(message.get("id") or message.get("message_id") or "").strip()
    }


def _live_gmail_envelopes_for_priority_grouping(
    args: argparse.Namespace,
) -> list[GmailMessageEnvelope]:
    gmail = GmailTool(live=True)
    label = _priority_grouping_source_label(args)
    query = str(args.gmail_query or "").strip() or f"newer_than:{args.lookback_days}d"
    summaries = _search_live_message_summaries(
        gmail,
        label=label,
        max_results=args.max_messages,
        query=query,
    )
    full_read_ids = [
        str(summary.get("id") or "").strip()
        for summary in summaries
        if str(summary.get("id") or "").strip() and _priority_summary_needs_full_message(summary)
    ]
    full_messages = _batch_read_live_messages(gmail, full_read_ids)
    envelopes: list[GmailMessageEnvelope] = []
    for summary in summaries:
        message_id = str(summary.get("id") or "").strip()
        if not message_id:
            continue
        message = full_messages.get(message_id)
        if message is None:
            if message_id in full_read_ids and not _summary_has_classification_context(summary):
                continue
            envelopes.append(_envelope_from_live_summary(summary))
            continue
        if not message.get("id"):
            message["id"] = message_id
        if _priority_summary_needs_thread(summary):
            try:
                envelopes.append(_thread_enriched_live_envelope(gmail, message))
                continue
            except ToolGuardrailViolation:
                pass
        envelope = _envelope_from_live_message_payload(message)
        limitations = [
            *envelope.triage_limitations,
            "GT-1 staged retrieval read the full message body without expanding the thread.",
        ]
        envelopes.append(
            envelope.model_copy(update={"triage_limitations": list(dict.fromkeys(limitations))})
        )
    return envelopes


def _priority_grouping_report_source(args: argparse.Namespace) -> str:
    if args.live_gmail:
        query = str(args.gmail_query or "").strip() or f"newer_than:{args.lookback_days}d"
        return (
            f"Read-only staged Gmail retrieval: search summaries for query {query} "
            f"with source label {_priority_grouping_source_label(args)}, batch-read "
            "shortlisted messages, and expand full threads only when needed."
        )
    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    names = ", ".join(Path(str(path)).name for path in fixture_paths)
    return f"Local Gmail fixtures: {names}."


def _priority_grouping_report_model(live: bool) -> str:
    if not live:
        return "injected fake/local SDK run_config"
    config = get_runtime_agent_model_config("gmail_triage").as_log_dict()
    provider = str(config.get("provider") or "unknown")
    model = str(config.get("model") or "unknown")
    return f"{provider}/{model}"


def _write_priority_grouping_test_pack_report(
    *,
    args: argparse.Namespace,
    result: GmailPriorityGroupingResult,
    live: bool,
    usage: dict[str, Any] | None = None,
    cost: dict[str, Any] | None = None,
    gemini_free_tier_usage: dict[str, Any] | None = None,
    orchestrator_review: dict[str, Any] | None = None,
) -> dict[str, str]:
    report_dir_value = str(args.test_pack_report_dir or "").strip()
    if not report_dir_value:
        return {}

    report_dir = Path(report_dir_value)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_payload = build_gmail_priority_grouping_test_pack_payload(
        result,
        run_type="live SDK" if live else "local SDK",
        model=_priority_grouping_report_model(live),
        input_summary=str(args.request or "").strip() or GT1_PRIORITY_GROUPING_PROMPT,
        input_source=_priority_grouping_report_source(args),
        usage=usage,
        cost=cost,
        gemini_free_tier_usage=gemini_free_tier_usage,
        orchestrator_review=orchestrator_review,
    )
    markdown_path = report_dir / "gmail-triage-gt1-priority-grouping.md"
    json_path = report_dir / "gmail-triage-gt1-priority-grouping.json"
    markdown_path.write_text(
        render_gmail_priority_grouping_test_pack_report(report_payload),
        encoding="utf-8",
    )
    json_path.write_text(to_json(report_payload), encoding="utf-8")
    return {
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "status": str(report_payload["status"]),
    }


def _run_priority_grouping_sdk_synthesis(
    args: argparse.Namespace,
    *,
    email_style_profile: Any | None,
    founder_fit_profile: Any | None,
) -> dict[str, Any]:
    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
            "create_draft": "--create-draft",
            "apply_labels": "--apply-labels",
            "preview_labels": "--preview-labels",
            "cleanup_labels": "--cleanup-labels",
        },
    )
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="read-only Gmail API calls for priority grouping",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    style_context = _style_profile_context(email_style_profile)
    founder_context = founder_drafting_context(founder_fit_profile)
    preflight_context = orchestrator_preflight_context_text(args)
    request_context = _priority_grouping_request_context(args, preflight_context)
    source_label = _priority_grouping_source_label(args)

    def retrieve() -> list[GmailMessageEnvelope]:
        if args.live_gmail:
            return _live_gmail_envelopes_for_priority_grouping(args)
        return _fixture_envelopes_for_priority_grouping(args)

    def normalize(envelopes: list[GmailMessageEnvelope]) -> GmailPriorityGroupingSDKInput:
        return GmailPriorityGroupingSDKInput.from_envelopes(
            envelopes,
            request=request_context,
            operator_request=str(getattr(args, "request", "") or "").strip(),
            lookback_days=args.lookback_days,
            source_label=source_label,
            email_style_profile=style_context,
            founder_fit_context=founder_context,
        )

    fixture_paths = list(args.fixtures or ([] if args.fixture is None else [args.fixture]))
    storage = StorageTool(args.database_url) if args.save else None
    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_priority_grouping_agent(),
        output_type=GmailPriorityGroupingResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary="gmail GT-1 priority grouping SDK synthesis",
        input_audit_payload={
            "priority_grouping": True,
            "request": request_context,
            "lookback_days": args.lookback_days,
            "source_label": source_label,
            "max_messages": args.max_messages,
            "live_gmail": bool(args.live_gmail),
            "fixture_count": len(fixture_paths),
            "fixture_names": [Path(str(path)).name for path in fixture_paths],
            "email_style_profile_fixture": args.email_style_profile_fixture,
            "email_style_profile_id": args.email_style_profile_id,
            "founder_fit_profile": founder_profile_audit_payload(
                args.founder_fit_profile,
                founder_fit_profile,
            ),
            "sdk_synthesis": True,
        },
        workflow_name="Keystone Gmail Priority Grouping SDK synthesis",
        trace_metadata={
            "workflow_kind": "gmail_priority_grouping",
            "lookback_days": args.lookback_days,
            "source_label": source_label,
        },
        run_config=run_config,
        live=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
        save=args.save,
        storage=storage,
        model_label="sdk-live" if live else "sdk-local",
    )
    authoritative_summary = str(getattr(args, "request", "") or "").strip() or request_context
    normalized_output = outcome.final_output.model_copy(
        update={
            "request_summary": authoritative_summary,
            "source_label": source_label,
            "lookback_days": args.lookback_days,
            "source_message_count": len(outcome.typed_input.messages),
        }
    )
    outcome = replace(outcome, result=replace(outcome.result, output=normalized_output))
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    review_payload = None
    if args.orchestrator_review:
        review_payload = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=outcome.final_output,
            request_summary=request_context,
            run_type="live SDK" if live else "local SDK",
        )
        payload["orchestrator_review"] = review_payload
    attach_orchestrator_preflight_payload(payload, args)
    payload["priority_grouping"] = True
    report = _write_priority_grouping_test_pack_report(
        args=args,
        result=outcome.final_output,
        live=live,
        usage=outcome.usage,
        cost=outcome.cost,
        gemini_free_tier_usage=payload.get("gemini_free_tier_usage"),
        orchestrator_review=review_payload,
    )
    if report:
        payload["test_pack_report"] = report
    return payload


def _run_contact_lookup_sdk_synthesis(args: argparse.Namespace) -> dict[str, Any]:
    """Retrieve bounded Gmail candidates, then let the SDK specialist interpret them."""

    reject_sdk_side_effect_flags(
        args,
        {
            "live_slack": "--live-slack",
            "request_approval": "--request-approval",
            "create_draft": "--create-draft",
            "update_draft": "--update-draft",
            "apply_labels": "--apply-labels",
            "preview_labels": "--preview-labels",
            "cleanup_labels": "--cleanup-labels",
        },
    )
    if not str(args.request or "").strip():
        raise RuntimeError("--contact-lookup requires the current operator --request.")
    if not str(args.gmail_query or "").strip():
        raise RuntimeError(
            "--contact-lookup requires a bounded --gmail-query; broad mailbox reads "
            "are not allowed for contact resolution."
        )
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="read-only Gmail contact evidence retrieval",
        )
    run_config, live = resolve_sdk_execution(
        args,
        run_config_factory=SDK_RUN_CONFIG_FACTORY,
    )
    query = str(args.gmail_query or "").strip()
    execution = run_gmail_contact_lookup_workflow(
        operator_request=str(args.request or "").strip(),
        gmail_query=query,
        max_messages=args.max_messages,
        label=args.label_filter,
        gmail_tool=GmailTool(live=True) if args.live_gmail else None,
        run_config=run_config,
        live_sdk=live,
        trace_include_sensitive_data=args.trace_include_sensitive_data,
    )
    outcome = execution.outcome
    result = execution.result
    receipt = execution.provider_receipt
    human_summary = execution.human_summary
    payload = sdk_synthesis_payload(
        outcome,
        include_provider_cost_window=args.include_provider_cost_window,
        provider_cost_window_seconds=args.provider_cost_window_seconds,
        openai_cost_project_id=args.openai_cost_project_id,
    )
    payload.update(
        {
            "status": "completed",
            "contact_lookup": True,
            "human_summary": human_summary,
            "tool_receipts": [receipt],
            "retrieval_diagnostics": {
                "provider": "gmail",
                "query": query,
                "candidate_count": len(outcome.typed_input.candidates),
                "selected_count": len(result.contacts),
                "provider_read": True,
                "provider_write": False,
            },
            "user_facing_result_verified": True,
            "public_result": {
                "status": "completed",
                "completion_confirmed": True,
                "provider_write_attempted": False,
                "provider_receipt_verified": True,
                "summary": human_summary,
            },
            "side_effects": {
                "gmail_read": True,
                "gmail_write": False,
                "email_sent": False,
            },
        }
    )
    attach_orchestrator_preflight_payload(payload, args)
    return payload


def _priority_grouping_request_context(args: argparse.Namespace, preflight_context: str) -> str:
    operator_request = str(getattr(args, "request", "") or "").strip()
    return "\n\n".join(
        item
        for item in (
            f"Operator request: {operator_request}" if operator_request else "",
            GT1_PRIORITY_GROUPING_PROMPT,
            preflight_context,
        )
        if item
    )


def _run_live_thread_summary(
    *,
    args: argparse.Namespace,
    gmail: Any,
    label: str,
) -> dict[str, Any]:
    if args.create_draft or args.apply_labels or args.preview_labels or args.cleanup_labels:
        return _structured_gmail_clarification(
            status="blocked",
            operation="thread_summary",
            clarification_request=(
                "Remove draft or label mutation flags and rerun the thread summary "
                "in read-only mode."
            ),
            message=(
                "Gmail thread summary mode is read-only and cannot create drafts or modify labels."
            ),
            source_label=label,
            query=args.gmail_query or "",
            reason_code="thread_summary_read_only",
            missing_inputs=[],
            suggested_next_steps=[
                "Rerun with --thread-summary only, or switch back to per-message "
                "triage for draft work."
            ],
        )

    if args.thread_id:
        thread_ids = [str(args.thread_id)]
    else:
        message_refs = gmail.list_recent_messages(
            label=label,
            max_results=args.max_messages,
            query=args.gmail_query,
        )
        thread_ids = _dedupe_thread_ids(message_refs)

    if not thread_ids:
        return _structured_gmail_clarification(
            status="blocked",
            operation="thread_summary",
            clarification_request=(
                "Broaden the Gmail query or pick a specific thread id before retrying."
            ),
            message="No Gmail thread matched the requested label and query.",
            source_label=label,
            query=args.gmail_query or "",
            reason_code="no_thread_found",
            suggested_next_steps=[
                "Adjust --gmail-query or pass --thread-id for a known thread.",
                "If you need sent mail, pass --label-filter SENT explicitly.",
            ],
        )

    summaries = [
        _thread_summary_result_from_payload(
            thread=gmail.get_thread(thread_id),
            source_label=label,
            query=args.gmail_query or "",
        ).model_dump(mode="json")
        for thread_id in thread_ids
    ]
    return {
        "mode": "live-gmail-thread-summary",
        "label": label,
        "query": args.gmail_query or "",
        "count": len(summaries),
        "threads": summaries,
        "send_enabled": False,
        "draft_created": False,
        "labels_modified": False,
    }


def _run_live_message_count(
    *,
    args: argparse.Namespace,
    gmail: GmailTool,
) -> dict[str, Any]:
    """Return one receipt-backed exact count for a typed Gmail collection read."""

    receipt = gmail.count_messages(
        label=args.label_filter or None,
        query=args.gmail_query or "",
    )
    complete = receipt.get("complete") is True
    count = int(receipt.get("message_count") or 0)
    direction = str(args.mailbox_direction or "unspecified")
    date_scope = str(args.date_scope or "unspecified")
    if complete:
        if direction == "inbound" and date_scope == "today":
            summary = f"You received {count} email{'s' if count != 1 else ''} today."
        elif direction == "outbound" and date_scope == "today":
            summary = f"You sent {count} email{'s' if count != 1 else ''} today."
        else:
            summary = f"I found {count} matching email{'s' if count != 1 else ''}."
        status = "completed"
        block_kind = ""
    else:
        summary = (
            f"I counted at least {count} matching emails, but the bounded provider read "
            "did not exhaust every Gmail result page, so I cannot report an exact total."
        )
        status = "blocked"
        block_kind = "gmail_count_incomplete"
    provider_receipt = {
        **receipt,
        "operation": "message_count",
        "mailbox_direction": direction,
        "date_scope": date_scope,
        "timezone": str(args.provider_timezone or "America/New_York"),
        "window_start": str(args.window_start or ""),
        "window_end": str(args.window_end or ""),
        "verified": complete,
    }
    output = {
        "status": status,
        "summary": summary,
        "message_count": count,
        "mailbox_direction": direction,
        "date_scope": date_scope,
        "timezone": provider_receipt["timezone"],
        "window_start": provider_receipt["window_start"],
        "window_end": provider_receipt["window_end"],
        "provider_read": True,
        "provider_write": False,
        "send_enabled": False,
        "draft_created": False,
        "labels_modified": False,
    }
    public_result = {
        "schema_name": "keystone.execution_public_result.v1",
        "status": status,
        "title": "Business Agents Result Ready" if complete else "Business Agents Blocked",
        "omit_title": False,
        "text": summary,
        "completion_confirmed": complete,
        "provider_write_attempted": False,
        "provider_receipt_verified": complete,
        "recovery_used": False,
        "recovery_notice": "",
        "failure_code": block_kind,
        "failure_summary": "" if complete else summary,
        "run_id": "",
    }
    return {
        "mode": "live-gmail-message-count",
        "status": status,
        "block_kind": block_kind,
        "send_enabled": False,
        "human_summary": summary,
        "output_type": "GmailMessageCountResult",
        "output": output,
        "retrieval_diagnostics": provider_receipt,
        "tool_receipts": [provider_receipt],
        "user_facing_result_verified": complete,
        "public_result": public_result,
        "side_effects": {
            "gmail_draft_created": False,
            "gmail_draft_updated": False,
            "labels_modified": False,
            "email_sent": False,
            "send_enabled": False,
        },
    }


def _run_live_message_projection(
    *,
    args: argparse.Namespace,
    gmail: GmailTool,
) -> dict[str, Any]:
    """Return selected metadata for the exact complete Gmail result set."""

    receipt = gmail.project_message_summaries(
        requested_fields=list(args.requested_field or []),
        label=args.label_filter or None,
        query=args.gmail_query or "",
        max_items=args.max_messages,
    )
    complete = receipt.get("complete") is True
    count = int(receipt.get("item_count") or 0)
    expected_count = args.expected_result_count
    count_matches = expected_count is None or count == expected_count
    verified = complete and count_matches
    items = receipt.get("items") if verified else []
    if not isinstance(items, list):
        items = []
    if verified:
        lines = [_render_gmail_projection_item(item, args.requested_field) for item in items]
        summary = "\n".join(f"- {line}" for line in lines) if lines else "No matching emails."
        status = "completed"
        block_kind = ""
    elif not complete:
        summary = (
            "I could not exhaust the bounded Gmail result set, so I did not return a partial list."
        )
        status = "blocked"
        block_kind = "gmail_projection_incomplete"
    else:
        summary = (
            "The Gmail result set changed after the prior verified read, so I did not "
            "claim this was the same set."
        )
        status = "blocked"
        block_kind = "gmail_projection_scope_changed"
    provider_receipt = {
        **receipt,
        "operation": "message_projection",
        "mailbox_direction": str(args.mailbox_direction or "unspecified"),
        "date_scope": str(args.date_scope or "unspecified"),
        "timezone": str(args.provider_timezone or "America/New_York"),
        "window_start": str(args.window_start or ""),
        "window_end": str(args.window_end or ""),
        "expected_result_count": expected_count,
        "verified": verified,
    }
    output = {
        "status": status,
        "summary": summary,
        "items": items,
        "item_count": count,
        "requested_fields": list(args.requested_field or []),
        "provider_read": True,
        "provider_write": False,
        "send_enabled": False,
        "draft_created": False,
        "labels_modified": False,
    }
    return {
        "mode": "live-gmail-message-projection",
        "status": status,
        "block_kind": block_kind,
        "send_enabled": False,
        "human_summary": summary,
        "output_type": "GmailMessageProjectionResult",
        "output": output,
        "retrieval_diagnostics": provider_receipt,
        "tool_receipts": [provider_receipt],
        "user_facing_result_verified": verified,
        "public_result": {
            "schema_name": "keystone.execution_public_result.v1",
            "status": status,
            "title": ("Business Agents Result Ready" if verified else "Business Agents Blocked"),
            "omit_title": False,
            "text": summary,
            "completion_confirmed": verified,
            "provider_write_attempted": False,
            "provider_receipt_verified": verified,
            "recovery_used": False,
            "recovery_notice": "",
            "failure_code": block_kind,
            "failure_summary": "" if verified else summary,
            "run_id": "",
        },
        "side_effects": {
            "gmail_draft_created": False,
            "gmail_draft_updated": False,
            "labels_modified": False,
            "email_sent": False,
            "send_enabled": False,
        },
    }


def _render_gmail_projection_item(item: object, fields: list[str]) -> str:
    if not isinstance(item, dict):
        return ""
    values = [str(item.get(field) or "").strip() for field in fields]
    return " | ".join(value for value in values if value)


@with_cli_environment()
def main() -> int:
    args = apply_orchestrator_preflight_to_args(
        _apply_live_test_defaults(build_parser().parse_args())
    )
    if args.max_messages < 1:
        raise SystemExit("--max-messages must be at least 1.")
    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be at least 1.")
    if args.test_pack_report_dir and not args.priority_grouping:
        raise SystemExit("--test-pack-report-dir is only supported with --priority-grouping.")
    if (args.message_count or args.message_projection or args.contact_lookup) and any(
        (
            args.create_draft,
            args.update_draft,
            args.apply_labels,
            args.preview_labels,
            args.cleanup_labels,
            args.request_approval,
            args.live_slack,
        )
    ):
        raise SystemExit("Gmail collection reads cannot be combined with Gmail mutations.")
    if args.message_projection and not args.requested_field:
        raise SystemExit("--message-projection requires at least one --requested-field.")
    email_style_profile = _load_requested_style_profile(args)
    founder_fit_profile = load_founder_fit_profile(args.founder_fit_profile)

    if sdk_execution_requested(args):
        try:
            if args.contact_lookup:
                payload = _run_contact_lookup_sdk_synthesis(args)
            elif args.priority_grouping:
                payload = _run_priority_grouping_sdk_synthesis(
                    args,
                    email_style_profile=email_style_profile,
                    founder_fit_profile=founder_fit_profile,
                )
            else:
                payload = _run_sdk_synthesis(
                    args,
                    email_style_profile=email_style_profile,
                    founder_fit_profile=founder_fit_profile,
                )
        except GmailTargetResolutionError as exc:
            payload = exc.payload
            attach_orchestrator_preflight_payload(payload, args)
        except GmailAgentDecisionError as exc:
            model_config = get_runtime_agent_model_config("gmail_triage")
            decision_live = bool(getattr(args, "live_sdk", False))
            payload = {
                "mode": "sdk-synthesis",
                "agent_name": "gmail_triage",
                "dry_run": not decision_live,
                "live_sdk": decision_live,
                "sdk_run_invoked": True,
                "model": {
                    "provider": model_config.provider,
                    "name": model_config.model,
                    "run_mode": "live_sdk" if decision_live else "sdk",
                },
                "status": "blocked",
                "block_kind": "gmail_agent_decision_validation_failed",
                "send_enabled": False,
                "human_summary": (
                    "Gmail Triage could not produce one provider-bound selection after "
                    "the allowed repair attempt, so it did not guess or create a draft."
                ),
                "decision_ownership": exc.telemetry,
                "output": {
                    "summary": (
                        "The Gmail candidate decision did not pass identity validation."
                    ),
                    "send_enabled": False,
                    "draft_created": False,
                },
            }
            _attach_gmail_decision_block_evidence(payload, exc)
            attach_orchestrator_preflight_payload(payload, args)
            attach_execution_public_result(payload)
            if args.save:
                payload["storage"] = {
                    "agent_run": _save_agent_owned_gmail_run(
                        args,
                        payload=payload,
                        model_provider=model_config.provider,
                        model_name=model_config.model,
                        live=decision_live,
                    )
                }
        except RuntimeError as exc:
            if not sdk_run_failure_metadata(exc):
                raise SystemExit(str(exc)) from exc
            return _handle_uncaught_exception(
                exc,
                ["--json"] if args.json else sys.argv[1:],
            )
        if args.markdown and not args.json:
            lines = [
                "# Gmail SDK Synthesis",
                "",
                f"Agent: {payload['agent_name']}",
                f"Live SDK: {str(payload['live_sdk']).lower()}",
                "SDK run invoked: true",
            ]
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            if review_markdown:
                lines.extend(["", review_markdown])
            print("\n".join(lines))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    try:
        if args.live_gmail:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-gmail",
                live_action="Gmail API calls",
            )
        if args.live_slack:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-slack",
                live_action="posting approval notifications to Slack",
                required_credentials=SLACK_APPROVAL_CREDENTIALS,
            )
        if not args.live_gmail and not args.live_slack and not args.dry_run:
            require_cli_live_confirmation(
                dry_run=False,
                live_flag=False,
                flag_name="--live-gmail or --live-slack",
                live_action="live Gmail triage",
            )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.sdk:
        agent = (
            build_gmail_contact_lookup_agent()
            if args.contact_lookup
            else build_gmail_priority_grouping_agent()
            if args.priority_grouping
            else build_gmail_triage_agent()
        )
        payload = {
            "mode": "sdk-agent",
            "dry_run": True,
            "live_sdk": False,
            "priority_grouping": bool(args.priority_grouping),
            "contact_lookup": bool(args.contact_lookup),
            "agent": sdk_agent_description(agent),
            "audit_notes": [
                "--sdk constructs the agent boundary only.",
                "Use the shared SDK synthesis harness with an explicit run_config or live=True "
                "for model execution.",
            ],
        }
        if args.markdown and not args.json:
            print(
                "\n".join(
                    [
                        "# Gmail SDK Agent",
                        "",
                        f"Agent: {payload['agent']['name']}",
                        f"Output type: {payload['agent']['output_type']}",
                        "SDK run invoked: false",
                    ]
                )
            )
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    if args.priority_grouping:
        raise SystemExit("--priority-grouping requires --run-sdk or --live-sdk to invoke the LLM.")
    if args.contact_lookup:
        raise SystemExit("--contact-lookup requires --run-sdk or --live-sdk to invoke the LLM.")

    if args.live_gmail:
        if not args.label_filter and not args.allow_inbox:
            raise SystemExit("Use --label-filter for live Gmail, or pass --allow-inbox explicitly.")
        if args.cleanup_labels and not (args.preview_labels or args.apply_labels):
            raise SystemExit("--cleanup-labels requires --preview-labels or --apply-labels.")

        gmail = GmailTool(live=True)
        label = args.label_filter or "INBOX"
        try:
            if args.message_projection:
                payload = _run_live_message_projection(args=args, gmail=gmail)
            elif args.message_count:
                payload = _run_live_message_count(args=args, gmail=gmail)
            elif args.thread_summary or args.thread_id:
                payload = _run_live_thread_summary(args=args, gmail=gmail, label=label)
            else:
                message_refs = gmail.list_recent_messages(
                    label=label,
                    max_results=args.max_messages,
                    query=args.gmail_query,
                )
                if args.create_draft and not message_refs:
                    payload = _structured_gmail_clarification(
                        status="blocked",
                        operation="draft_reply",
                        clarification_request=(
                            "Adjust the label or Gmail query so one target message can be selected."
                        ),
                        message="No Gmail message matched the current draft request.",
                        source_label=label,
                        query=args.gmail_query or "",
                        reason_code="draft_target_not_found",
                        missing_inputs=["matching gmail message"],
                        suggested_next_steps=[
                            "Broaden --gmail-query, or pick a different label filter.",
                        ],
                    )
                elif args.create_draft and len(message_refs) != 1:
                    payload = _structured_gmail_clarification(
                        status="clarification_required",
                        operation="draft_reply",
                        clarification_request=(
                            "Narrow the request to one Gmail message with --gmail-query or use "
                            "--thread-id with --thread-summary before creating a draft."
                        ),
                        message=(
                            "Live Gmail draft creation requires exactly one selected message."
                        ),
                        source_label=label,
                        query=args.gmail_query or "",
                        reason_code="draft_target_ambiguous",
                        candidate_thread_ids=_dedupe_thread_ids(message_refs),
                        missing_inputs=["specific gmail_query or thread_id"],
                        suggested_next_steps=[
                            "Rerun with --max-messages 1 and a narrower --gmail-query.",
                            "Use --thread-summary --thread-id <id> to review the "
                            "full thread first.",
                        ],
                    )
                else:
                    items: list[dict[str, Any]] = []
                    for ref in message_refs:
                        message = gmail.get_message(str(ref["id"]))
                        if not message.get("id"):
                            message["id"] = str(ref["id"])
                        envelope = _thread_enriched_live_envelope(gmail, message)
                        triage = triage_gmail_message_envelope(
                            envelope,
                            email_style_profile=email_style_profile,
                        )
                        data = triage.model_dump()
                        data["approval_decision"] = (
                            "pending" if triage.draft_reply else "not_required"
                        )
                        data["approval_scope"] = ApprovalScope.SEND.value
                        if args.preview_labels or args.apply_labels:
                            data["labels_api"] = _apply_labels_with_optional_cleanup(
                                gmail,
                                message=message,
                                message_id=triage.message_id,
                                labels=triage.recommended_labels,
                                cleanup_obsolete=args.cleanup_labels,
                                dry_run_preview=args.preview_labels,
                            )
                        else:
                            data["labels_api"] = {
                                "status": "skipped",
                                "reason": (
                                    "Pass --preview-labels to preview label changes or "
                                    "--apply-labels to modify Gmail labels."
                                ),
                                "message_id": triage.message_id,
                                "labels": triage.recommended_labels,
                                "cleanup_obsolete": args.cleanup_labels,
                            }
                        if triage.draft_reply:
                            if args.create_draft:
                                data["draft_approval"] = _require_live_draft_approval(
                                    database_url=args.database_url,
                                    message_id=triage.message_id,
                                )
                                draft_api = gmail.create_draft_reply(
                                    message_id=triage.message_id,
                                    body=triage.draft_reply,
                                )
                                draft_api["sent"] = False
                                draft_api["approval_required"] = True
                                data["draft_api"] = draft_api
                            else:
                                data["draft_api"] = {
                                    "status": "skipped",
                                    "reason": "Pass --create-draft to create a Gmail draft.",
                                    "approval_required": True,
                                }
                        items.append(data)
                    payload = {
                        "mode": "live-gmail",
                        "label": label,
                        "query": args.gmail_query or "",
                        "count": len(items),
                        "messages": items,
                    }
        except (GmailAPIError, GmailConfigurationError) as exc:
            raise SystemExit(str(exc)) from exc

        if args.orchestrator_review:
            payload["orchestrator_review"] = build_cli_orchestrator_review(
                args,
                run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
                agent_name="gmail_triage",
                output=payload,
                request_summary=_orchestrator_review_request_summary(
                    args,
                    fallback=f"live Gmail triage for {label}",
                ),
                run_type="live Gmail",
            )
        attach_orchestrator_preflight_payload(payload, args)
        if args.markdown:
            if payload.get("mode") == "live-gmail-thread-summary":
                summary = _render_thread_summary_markdown(payload)
            elif payload.get("status") in {"clarification_required", "blocked"}:
                summary = _render_clarification_markdown(payload)
            else:
                messages = payload.get("messages", [])
                summary = (
                    "\n\n".join(item["summary"] for item in messages)
                    if messages
                    else "No messages found."
                )
            review_markdown = render_orchestrator_output_review(payload.get("orchestrator_review"))
            print("\n\n".join(item for item in (summary, review_markdown) if item))
        else:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0

    result = run_gmail_triage_fixture(
        args.fixture,
        subject=args.subject,
        sender_name=args.sender_name,
        sender_email=args.sender_email,
        email_style_profile=email_style_profile,
    )
    if args.approval_decision in TERMINAL_APPROVAL_DECISIONS and result.draft_reply:
        result = result.model_copy(
            update={
                "draft_reply": None,
                "draft_created": False,
                "approval_required": False,
                "recommended_action": (
                    f"Manual approval decision {args.approval_decision}; no draft created."
                ),
            }
        )
    data = result.model_dump()
    data["approval_decision"] = args.approval_decision
    data["approval_scope"] = args.approval_scope
    attach_orchestrator_preflight_payload(data, args)
    if args.orchestrator_review:
        data["orchestrator_review"] = build_cli_orchestrator_review(
            args,
            run_config_factory=ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY,
            agent_name="gmail_triage",
            output=result,
            request_summary=_orchestrator_review_request_summary(
                args,
                fallback=args.subject or args.fixture or "gmail triage fixture run",
            ),
            run_type="deterministic fixture",
        )
    if args.request_approval and (
        result.draft_reply or args.approval_decision in TERMINAL_APPROVAL_DECISIONS
    ):
        approval_request = post_approval_request(
            result,
            context={
                "object_type": "gmail_draft",
                "object_id": result.message_id,
                "summary": result.summary,
                "decision": args.approval_decision,
                "scope": args.approval_scope,
                "reviewer": args.reviewer,
                "notes": args.approval_notes,
            },
            live=args.live_slack,
        )
        data["approval_request"] = approval_request.model_dump()
        queue_item = build_approval_queue_item(
            result,
            context=_approval_context(
                object_id=result.message_id or "gmail-draft",
                summary=result.summary,
                args=args,
                slack_ts=approval_request.slack_ts,
            ),
        )
        data["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.save:
        storage = StorageTool(args.database_url)
        data["storage"] = {
            "email": storage.save_email(result),
            "agent_run": storage.save_agent_run(
                agent_name="gmail_triage",
                input_payload={
                    "fixture": args.fixture,
                    "subject": args.subject,
                    "sender_email": args.sender_email,
                    "email_style_profile_fixture": args.email_style_profile_fixture,
                    "email_style_profile_id": args.email_style_profile_id,
                },
                input_summary=args.subject or args.fixture or "gmail triage fixture run",
                output=data,
                model="fixture",
                dry_run=True,
                status="success",
            ),
        }
        if result.approval_required or args.approval_decision in TERMINAL_APPROVAL_DECISIONS:
            data["storage"]["approval"] = storage.save_approval(
                object_type="gmail_draft",
                object_id=result.message_id or data["storage"]["email"]["id"],
                decision=args.approval_decision,
                scope=args.approval_scope,
                reviewer=args.reviewer,
                notes=args.approval_notes,
                risk_flags=result.risk_flags,
                source_agent="gmail_triage",
            )
            queue_item = build_approval_queue_item(
                result,
                context=_approval_context(
                    object_id=result.message_id or data["storage"]["email"]["id"],
                    summary=result.summary,
                    args=args,
                    slack_ts=(
                        data.get("approval_request", {}).get("slack_ts")
                        if isinstance(data.get("approval_request"), dict)
                        else None
                    ),
                ),
            )
            data["storage"]["approval_queue"] = storage.save_approval_item(queue_item)
            data["approval_queue_item"] = queue_item.model_dump(mode="json")
    if args.markdown:
        report = render_gmail_triage_report(result)
        review_markdown = render_orchestrator_output_review(data.get("orchestrator_review"))
        print("\n\n".join(item for item in (report, review_markdown) if item))
    else:
        print(json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def _handle_uncaught_exception(exc: Exception, argv: list[str] | None = None) -> int:
    """Emit one audit-safe Gmail failure envelope instead of losing SDK evidence."""

    failure = known_exception_to_operator_failure(exc, context="Gmail Triage run")
    sdk_failure = sdk_run_failure_metadata(exc)
    if "--json" in set(argv or []):
        payload: dict[str, Any] = {
            "status": "failed",
            "agent_name": "gmail_triage",
            "send_enabled": False,
            "draft_created": False,
            "sent": False,
            "output": {
                "failure": failure.to_dict(),
                "summary": failure.summary,
                "next_step": failure.next_step,
                "send_enabled": False,
            },
        }
        if sdk_failure:
            payload.update(
                {
                    "sdk_failure": sdk_failure,
                    "usage": sdk_failure.get("usage") or {},
                    "cost": sdk_failure.get("cost") or {},
                    "request_cache": sdk_failure.get("request_cache") or {},
                    "tool_execution": sdk_failure.get("tool_execution") or {},
                    "tool_receipts": list(sdk_failure.get("tool_receipts") or []),
                    "execution_telemetry": sdk_failure.get("execution_telemetry") or {},
                }
            )
            request_cache = payload["request_cache"]
            if isinstance(request_cache, Mapping):
                decision = request_cache.get("decision_ownership")
                if isinstance(decision, Mapping):
                    payload["decision_ownership"] = dict(decision)
                request_budget = request_cache.get(
                    "model_request_budget",
                    request_cache.get("request_budget"),
                )
                if isinstance(request_budget, Mapping):
                    payload["request_budget"] = dict(request_budget)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    print(failure.summary, file=sys.stderr)
    print(f"Reason: {failure.reason}", file=sys.stderr)
    print(f"Next step: {failure.next_step}", file=sys.stderr)
    return 1


def _attach_gmail_decision_block_evidence(
    payload: dict[str, Any],
    exc: GmailAgentDecisionError,
) -> None:
    """Preserve audit-safe tool and model evidence for a normal decision block."""

    result = exc.result
    if result is None:
        return
    request_cache = dict(result.request_cache or {})
    request_cache["decision_ownership"] = dict(exc.telemetry)
    payload.update(
        {
            "usage": dict(result.usage or {}),
            "cost": dict(result.cost or {}),
            "budget_guard": dict(result.budget_guard or {}),
            "request_cache": request_cache,
            "tool_receipts": list(result.tool_receipts or []),
            "execution_telemetry": dict(result.execution_telemetry or {}),
        }
    )
    tool_execution = request_cache.get("tool_execution")
    if isinstance(tool_execution, Mapping):
        payload["tool_execution"] = dict(tool_execution)
    request_budget = request_cache.get(
        "model_request_budget",
        request_cache.get("request_budget"),
    )
    if isinstance(request_budget, Mapping):
        payload["request_budget"] = dict(request_budget)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(_handle_uncaught_exception(exc, sys.argv[1:])) from exc
