"""Bind Gmail agent decisions to the exact bounded provider evidence it read."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from keystone_agents.runtime.tool_execution import (
    sdk_tool_execution_records,
    sdk_tool_output_payloads,
)
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionTelemetryEvent,
    DecisionValidatorOutcome,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.gmail_query import (
    MAX_GMAIL_MODEL_QUERIES,
    GmailMessageSummaryRecord,
)


@dataclass(frozen=True)
class GmailDecisionEvidence:
    """Trace-safe identities and bounded summaries returned to one model run."""

    candidate_message_ids: tuple[str, ...]
    candidate_thread_ids: tuple[str, ...]
    read_message_ids: tuple[str, ...]
    read_thread_ids: tuple[str, ...]
    candidate_summaries: tuple[dict[str, Any], ...]
    read_context_summaries: tuple[dict[str, Any], ...]
    query_call_count: int
    query_output_count: int
    context_read_call_count: int
    context_read_output_count: int
    query_provider_read_performed: bool = False
    context_provider_read_count: int = 0
    query_attempts: tuple[dict[str, Any], ...] = ()
    corrective_query_count: int = 0
    model_query_call_count: int = 0
    repeated_query_call_count: int = 0
    blocked_query_call_count: int = 0
    model_context_read_call_count: int = 0
    repeated_context_read_call_count: int = 0
    blocked_context_read_call_count: int = 0
    rejected_context_read_call_count: int = 0

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_summaries)

    @property
    def query_thread_count(self) -> int:
        return len(tuple(dict.fromkeys(self.candidate_thread_ids)))

    @property
    def decision_candidate_ids(self) -> tuple[str, ...]:
        """Return the canonical identities whose context the model actually read."""

        candidate_messages = set(self.candidate_message_ids)
        candidate_threads = set(self.candidate_thread_ids)
        canonical_reads = {
            thread_id for thread_id in self.read_thread_ids if thread_id in candidate_threads
        }
        canonical_reads.update(
            self.canonical_candidate_id(message_id)
            for message_id in self.read_message_ids
            if message_id in candidate_messages
        )
        return tuple(
            thread_id for thread_id in self.candidate_thread_ids if thread_id in canonical_reads
        )

    def canonical_candidate_id(self, candidate_id: str) -> str:
        """Normalize one verified message identity to its conversation identity."""

        value = str(candidate_id or "").strip()
        if not value:
            return ""
        for candidate in self.candidate_summaries:
            if str(candidate.get("message_id") or "").strip() == value:
                return str(candidate.get("thread_id") or value).strip()
        return value

    def repair_context(self) -> dict[str, Any]:
        """Return the same bounded evidence without raw content or provider secrets."""

        return {
            "schema": "keystone.gmail.decision_evidence.v1",
            "candidate_count": self.candidate_count,
            "query_message_count": self.candidate_count,
            "query_thread_count": self.query_thread_count,
            "decision_candidate_count": len(self.decision_candidate_ids),
            "query_supporting_summaries": [dict(item) for item in self.candidate_summaries],
            "verified_contexts": [dict(item) for item in self.read_context_summaries],
            "decision_candidate_ids": list(self.decision_candidate_ids),
            "assessment_scope": (
                "Assess every identity in decision_candidate_ids. These are the "
                "bounded candidate contexts already read by the model. Query-only "
                "candidates that were not read do not require an assessment."
            ),
            "read_message_ids": list(self.read_message_ids),
            "read_thread_ids": list(self.read_thread_ids),
            "query_call_count": self.query_call_count,
            "query_output_count": self.query_output_count,
            "context_read_call_count": self.context_read_call_count,
            "context_read_output_count": self.context_read_output_count,
            "query_provider_read_performed": self.query_provider_read_performed,
            "context_provider_read_count": self.context_provider_read_count,
            "query_attempts": [dict(item) for item in self.query_attempts],
            "corrective_query_count": self.corrective_query_count,
            "model_query_call_count": self.model_query_call_count,
            "repeated_query_call_count": self.repeated_query_call_count,
            "blocked_query_call_count": self.blocked_query_call_count,
            "model_context_read_call_count": self.model_context_read_call_count,
            "repeated_context_read_call_count": self.repeated_context_read_call_count,
            "blocked_context_read_call_count": self.blocked_context_read_call_count,
            "rejected_context_read_call_count": self.rejected_context_read_call_count,
        }


def gmail_decision_evidence(
    raw_result: Any,
    *,
    cumulative_tool_evidence: Sequence[Mapping[str, Any]] = (),
) -> GmailDecisionEvidence:
    """Project query and context tool outputs into a validation candidate set."""

    records = sdk_tool_execution_records(raw_result)
    raw_query_call_count = sum(
        record.tool_name == "query_gmail_message_summaries" for record in records
    )
    raw_context_read_call_count = sum(
        record.tool_name == "read_gmail_context" for record in records
    )
    cumulative_queries = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "") == "query_gmail_message_summaries"
    ]
    cumulative_repeated_queries = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "")
        == "query_gmail_message_summaries_repeated"
    ]
    cumulative_blocked_queries = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "")
        == "query_gmail_message_summaries_blocked"
    ]
    cumulative_contexts = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "") == "read_gmail_context"
    ]
    cumulative_repeated_contexts = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "") == "read_gmail_context_repeated"
    ]
    cumulative_blocked_contexts = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "") == "read_gmail_context_blocked"
    ]
    cumulative_rejected_contexts = [
        dict(entry)
        for entry in cumulative_tool_evidence
        if str(entry.get("tool_name") or "") == "read_gmail_context_rejected"
    ]
    query_call_count = (
        len(cumulative_queries) if cumulative_tool_evidence else raw_query_call_count
    )
    model_query_call_count = max(
        raw_query_call_count,
        len(cumulative_queries)
        + len(cumulative_repeated_queries)
        + len(cumulative_blocked_queries),
    )
    model_context_read_call_count = max(
        raw_context_read_call_count,
        len(cumulative_contexts)
        + len(cumulative_repeated_contexts)
        + len(cumulative_blocked_contexts),
        len(cumulative_contexts)
        + len(cumulative_repeated_contexts)
        + len(cumulative_blocked_contexts)
        + len(cumulative_rejected_contexts),
    )
    candidates: list[dict[str, Any]] = []
    query_outputs = (
        [
            {"output": dict(entry.get("output") or {})}
            for entry in cumulative_queries
            if isinstance(entry.get("output"), Mapping)
        ]
        if cumulative_queries
        else sdk_tool_output_payloads(
            raw_result,
            tool_name="query_gmail_message_summaries",
        )
    )
    query_provider_read_performed = bool(
        query_outputs
        and all(
            isinstance(entry.get("output"), dict)
            and entry["output"].get("provider_read_performed") is True
            and str(entry["output"].get("status") or "") == "read"
            for entry in query_outputs
        )
    )
    query_attempts: list[dict[str, Any]] = []
    query_signatures: list[tuple[str, str, int]] = []
    for index, entry in enumerate(cumulative_queries, start=1):
        arguments = entry.get("arguments") or {}
        output = entry.get("output") or {}
        signature = (
            " ".join(str(arguments.get("query") or "").split()),
            " ".join(str(arguments.get("label") or "").split()),
            int(arguments.get("max_results") or 10),
        )
        query_signatures.append(signature)
        query_fingerprint = hashlib.sha256(
            "\x1f".join((signature[0], signature[1], str(signature[2]))).encode("utf-8")
        ).hexdigest()[:16]
        executed_queries = (
            output.get("executed_queries") or [signature[0]]
            if output.get("provider_read_performed") is True else []
        )
        query_attempts.append(
            {
                "attempt": index,
                "query_sha256": query_fingerprint,
                "query_present": bool(signature[0]),
                "label_present": bool(signature[1]),
                "max_results": signature[2],
                "returned_candidate_count": len(
                    [item for item in output.get("items") or [] if isinstance(item, Mapping)]
                ),
                "provider_read_performed": output.get("provider_read_performed") is True,
                "provider_search_count": len(executed_queries),
                "executed_query_sha256": [
                    hashlib.sha256(
                        "\x1f".join((query, signature[1], str(signature[2]))).encode("utf-8")
                    ).hexdigest()[:16]
                    for query in executed_queries
                ],
            }
        )
    corrective_query_count = sum(
        signature != query_signatures[index - 1]
        for index, signature in enumerate(query_signatures[1:], start=1)
    )
    for entry in query_outputs:
        output = entry.get("output") or {}
        for item in output.get("items") or []:
            if not isinstance(item, dict):
                continue
            message_id = str(item.get("message_id") or "").strip()
            thread_id = str(item.get("thread_id") or "").strip()
            if not message_id or not thread_id:
                continue
            candidate = {
                "message_id": message_id,
                "thread_id": thread_id,
                "received_at": str(item.get("received_at") or "")[:40],
                "sender_name": str(item.get("sender_name") or "")[:200],
                "sender_email": str(item.get("sender_email") or "")[:320],
                "subject": str(item.get("subject") or "")[:300],
                "snippet": str(item.get("snippet") or "")[:500],
                "prior_labels": [str(label)[:100] for label in item.get("prior_labels") or []][:20],
            }
            if candidate not in candidates:
                candidates.append(candidate)

    read_messages: list[str] = []
    read_threads: list[str] = []
    read_context_summaries: list[dict[str, Any]] = []
    context_outputs = (
        [
            {"output": dict(entry.get("output") or {})}
            for entry in cumulative_contexts
            if isinstance(entry.get("output"), Mapping)
        ]
        if cumulative_contexts
        else sdk_tool_output_payloads(raw_result, tool_name="read_gmail_context")
    )
    usable_context_identities: set[tuple[str, str]] = set()
    context_provider_read_count = 0
    for entry in context_outputs:
        output = entry.get("output") or {}
        if str(output.get("status") or "") not in {"fixture", "read"}:
            continue
        if (
            output.get("provider_read_performed") is True
            and str(output.get("status") or "") == "read"
        ):
            context_provider_read_count += 1
        resource_id = str(output.get("resource_id") or "").strip()
        resource_type = str(output.get("resource_type") or "").strip()
        if resource_id and resource_type in {"message", "thread"}:
            usable_context_identities.add((resource_type, resource_id))
            timeline = []
            for item in (output.get("messages") or [])[:12]:
                if not isinstance(item, Mapping):
                    continue
                try:
                    message_summary = GmailMessageSummaryRecord.model_validate(item)
                except ValueError:
                    continue
                if resource_type == "thread" and message_summary.thread_id != resource_id:
                    continue
                timeline.append(message_summary.model_dump(mode="json"))
            read_context_summaries.append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "subject": str(output.get("subject") or "")[:300],
                    "summary": str(output.get("summary") or "")[:800],
                    "thread_context": str(output.get("thread_context") or "")[:1200],
                    "latest_received_at": str(output.get("latest_received_at") or "")[:40],
                    "messages": timeline,
                    "source_url": str(output.get("source_url") or "")[:2048],
                    "triage_limitations": [str(item)[:500]
                                           for item in output.get("triage_limitations") or []][:20],
                    "participants": [str(item)[:320] for item in output.get("participants") or []][
                        :12
                    ],
                }
            )
        if resource_type == "message" and resource_id:
            read_messages.append(resource_id)
        elif resource_type == "thread" and resource_id:
            read_threads.append(resource_id)
        message = output.get("message")
        if isinstance(message, dict):
            message_id = str(message.get("message_id") or "").strip()
            thread_id = str(message.get("thread_id") or "").strip()
            if message_id:
                read_messages.append(message_id)
            if thread_id:
                read_threads.append(thread_id)

    return GmailDecisionEvidence(
        candidate_message_ids=tuple(dict.fromkeys(str(item["message_id"]) for item in candidates)),
        candidate_thread_ids=tuple(dict.fromkeys(str(item["thread_id"]) for item in candidates)),
        read_message_ids=tuple(dict.fromkeys(read_messages)),
        read_thread_ids=tuple(dict.fromkeys(read_threads)),
        candidate_summaries=tuple(candidates[:40]),
        read_context_summaries=tuple(read_context_summaries[:4]),
        query_call_count=query_call_count,
        query_output_count=len(query_outputs),
        context_read_call_count=len(usable_context_identities),
        context_read_output_count=len(usable_context_identities),
        query_provider_read_performed=query_provider_read_performed,
        context_provider_read_count=context_provider_read_count,
        query_attempts=tuple(query_attempts),
        corrective_query_count=corrective_query_count,
        model_query_call_count=model_query_call_count,
        repeated_query_call_count=len(cumulative_repeated_queries),
        blocked_query_call_count=len(cumulative_blocked_queries),
        model_context_read_call_count=model_context_read_call_count,
        repeated_context_read_call_count=len(cumulative_repeated_contexts),
        blocked_context_read_call_count=len(cumulative_blocked_contexts),
        rejected_context_read_call_count=len(cumulative_rejected_contexts),
    )


def validate_gmail_agent_decision(
    result: EmailTriageResult,
    evidence: GmailDecisionEvidence,
    *,
    original_request: str = "",
    repair_attempted: bool = False,
    require_live_provider: bool = False,
) -> DecisionValidatorOutcome:
    """Validate identity and coverage; never replace the model's selection."""

    decision = result.decision
    selected_message_id = str(result.message_id or "").strip()
    selected_thread_id = str(result.thread_id or "").strip()
    decision_ids = tuple(decision.selected_candidate_ids)
    decision_id = str(decision.selected_candidate_id or "").strip()
    valid_query_shape = bool(
        evidence.model_query_call_count <= MAX_GMAIL_MODEL_QUERIES + 1
        and evidence.repeated_query_call_count <= 1
        and 1 <= evidence.query_call_count <= MAX_GMAIL_MODEL_QUERIES
        and evidence.corrective_query_count == evidence.query_call_count - 1
    )
    if not valid_query_shape:
        return _repair_outcome(
            "gmail_query_call_count_out_of_bounds",
            (
                "Use one bounded Gmail summary query and, only when needed, up to two "
                "distinct corrective queries. Do not repeat an identical query."
            ),
            evidence,
            repair_attempted=repair_attempted,
        )
    if evidence.query_output_count != evidence.query_call_count:
        return _repair_outcome(
            "gmail_query_output_missing_or_malformed",
            "Every bounded Gmail query must return a structured result before selection.",
            evidence,
            repair_attempted=repair_attempted,
        )
    if require_live_provider and not evidence.query_provider_read_performed:
        return _repair_outcome(
            "gmail_live_query_evidence_missing",
            (
                "The requested live Gmail selection did not contain a verified "
                "provider query result; fixture output cannot support a live decision."
            ),
            evidence,
            repair_attempted=repair_attempted,
        )
    if evidence.context_read_call_count > 4:
        return _repair_outcome(
            "gmail_context_read_count_out_of_bounds",
            "Read no more than four plausible Gmail message/thread contexts.",
            evidence,
            repair_attempted=repair_attempted,
        )
    if not _gmail_decision_was_explicitly_returned(result):
        return _repair_outcome(
            "decision_record_not_explicitly_returned",
            (
                "Return an explicit Gmail decision record. Python will not infer a "
                "selection from message_id or thread_id."
            ),
            evidence,
            repair_attempted=repair_attempted,
        )
    if decision.decision_owner != "specialist_agent":
        return _repair_outcome(
            "gmail_decision_owner_mismatch",
            "Gmail candidate selection must use decision_owner=specialist_agent.",
            evidence,
            selected_candidate_id=decision_id,
            repair_attempted=repair_attempted,
        )
    if decision.decision_stage != "gmail_candidate_selection":
        return _repair_outcome(
            "gmail_decision_stage_mismatch",
            "Gmail candidate selection must use decision_stage=gmail_candidate_selection.",
            evidence,
            selected_candidate_id=decision_id,
            repair_attempted=repair_attempted,
        )
    if not str(decision.reasoning or "").strip():
        return _repair_outcome(
            "gmail_decision_reasoning_missing",
            "Explain why the selected Gmail candidate is the best match.",
            evidence,
            selected_candidate_id=decision_id,
            repair_attempted=repair_attempted,
        )
    if decision.needs_more_context:
        claimed_fields = _claimed_gmail_output_fields(result)
        if decision_ids or claimed_fields:
            return _repair_outcome(
                "needs_more_context_with_claimed_gmail_output",
                (
                    "A needs_more_context decision cannot also select an identity or "
                    "claim provider-specific triage, reply, label, or draft output."
                ),
                evidence,
                selected_candidate_id=decision_id,
                repair_attempted=repair_attempted,
            )
        return DecisionValidatorOutcome(
            status="accepted",
            decision_stage="gmail_candidate_selection",
            candidate_count=evidence.candidate_count,
            reason_code="agent_requested_more_context",
            feedback="The agent explicitly declined to guess among the bounded candidates.",
            repair_attempted=repair_attempted,
        )
    if not decision_ids:
        return _repair_outcome(
            "missing_decision_selection",
            (
                "Set decision.selected_candidate_ids to exactly one returned Gmail "
                "identity and keep decision.selected_candidate_id consistent with it, "
                "or set needs_more_context=true without claiming output."
            ),
            evidence,
            repair_attempted=repair_attempted,
        )
    if len(decision_ids) != 1:
        return _repair_outcome(
            "multiple_gmail_decision_selections",
            "Select exactly one Gmail message or thread identity for this triage result.",
            evidence,
            selected_candidate_id=decision_id,
            repair_attempted=repair_attempted,
        )
    if not (selected_message_id or selected_thread_id):
        return _repair_outcome(
            "selected_output_identity_missing",
            "Return the message_id or thread_id selected by the explicit decision.",
            evidence,
            selected_candidate_id=decision_id,
            repair_attempted=repair_attempted,
        )

    message_in_set = bool(
        selected_message_id and selected_message_id in evidence.candidate_message_ids
    )
    thread_in_set = bool(selected_thread_id and selected_thread_id in evidence.candidate_thread_ids)
    if not (message_in_set or thread_in_set):
        return _repair_outcome(
            "fabricated_selected_identity",
            "The selected identity was not present in the Gmail query result set.",
            evidence,
            selected_candidate_id=decision_id or selected_thread_id or selected_message_id,
            selected_identity_in_candidate_set=False,
            selected_identity_was_read=False,
            repair_attempted=repair_attempted,
        )
    if selected_message_id and selected_thread_id:
        pair_in_set = any(
            item["message_id"] == selected_message_id and item["thread_id"] == selected_thread_id
            for item in evidence.candidate_summaries
        )
        if not pair_in_set:
            return _repair_outcome(
                "contradictory_selected_identity",
                (
                    "The selected message_id and thread_id do not belong to the "
                    "same returned candidate."
                ),
                evidence,
                selected_candidate_id=decision_id or selected_thread_id or selected_message_id,
                selected_identity_in_candidate_set=True,
                repair_attempted=repair_attempted,
            )
    selected_was_read = bool(
        (selected_message_id and selected_message_id in evidence.read_message_ids)
        or (selected_thread_id and selected_thread_id in evidence.read_thread_ids)
    )
    if not selected_was_read:
        return _repair_outcome(
            "selected_candidate_not_read",
            "Read the selected message or thread context before deciding reply relevance.",
            evidence,
            selected_candidate_id=decision_id or selected_thread_id or selected_message_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=False,
            repair_attempted=repair_attempted,
        )
    if require_live_provider and evidence.context_provider_read_count < 1:
        return _repair_outcome(
            "gmail_live_context_evidence_missing",
            (
                "The selected candidate was not grounded in a verified live Gmail "
                "context read; fixture context cannot support a live decision."
            ),
            evidence,
            selected_candidate_id=decision_id or selected_thread_id or selected_message_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )

    selected_candidate_id = evidence.canonical_candidate_id(
        selected_thread_id or selected_message_id
    )
    if decision_id not in {selected_message_id, selected_thread_id}:
        return _repair_outcome(
            "decision_identity_mismatch",
            "The decision.selected_candidate_id must match the selected message_id or thread_id.",
            evidence,
            selected_candidate_id=decision_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )

    assessed: dict[str, str] = {}
    verified_assessment_ids = set(evidence.candidate_message_ids).union(
        evidence.candidate_thread_ids
    )
    for item in decision.candidate_assessments:
        if item.candidate_id not in verified_assessment_ids:
            return _repair_outcome(
                "candidate_assessment_identity_not_in_set",
                "Every candidate assessment must use an exact returned Gmail identity.",
                evidence,
                selected_candidate_id=selected_candidate_id,
                selected_identity_in_candidate_set=True,
                selected_identity_was_read=True,
                repair_attempted=repair_attempted,
            )
        canonical_id = evidence.canonical_candidate_id(item.candidate_id)
        prior = assessed.get(canonical_id)
        if prior is not None and prior != item.disposition:
            return _repair_outcome(
                "contradictory_candidate_assessments",
                "Use one consistent disposition for each verified Gmail conversation.",
                evidence,
                selected_candidate_id=selected_candidate_id,
                selected_identity_in_candidate_set=True,
                selected_identity_was_read=True,
                repair_attempted=repair_attempted,
            )
        assessed[canonical_id] = item.disposition
    all_candidate_ids = set(evidence.decision_candidate_ids)
    if len(all_candidate_ids) > 1 and not all_candidate_ids.issubset(assessed):
        return _repair_outcome(
            "candidate_assessments_incomplete",
            (
                "Assess every Gmail candidate context that was actually read and "
                "explain why each non-selected alternative was excluded."
            ),
            evidence,
            selected_candidate_id=selected_candidate_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )
    if assessed.get(selected_candidate_id) != "selected":
        return _repair_outcome(
            "selected_candidate_assessment_missing",
            "Mark the selected Gmail identity as selected in candidate_assessments.",
            evidence,
            selected_candidate_id=selected_candidate_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )
    if any(
        assessed.get(candidate_id) != "excluded"
        for candidate_id in all_candidate_ids
        if candidate_id != selected_candidate_id
    ):
        return _repair_outcome(
            "gmail_alternatives_not_excluded",
            "Mark every non-selected Gmail candidate excluded and explain why.",
            evidence,
            selected_candidate_id=selected_candidate_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )

    if _gmail_reply_lifecycle_conflicts_with_request(
        original_request=original_request,
        result=result,
    ):
        return _repair_outcome(
            "gmail_reply_lifecycle_mismatch",
            (
                "Re-evaluate reply relevance and wording against the complete operator "
                "request and the selected context timestamps. The request describes a "
                "completed event, so do not present stale pre-event scheduling language "
                "such as 'tomorrow' or 'is set' as the current state. Keep the same "
                "provider evidence, but repair the semantic decision or explicitly say "
                "that more context is needed."
            ),
            evidence,
            selected_candidate_id=selected_candidate_id,
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            repair_attempted=repair_attempted,
        )

    return DecisionValidatorOutcome(
        status="accepted",
        decision_stage="gmail_candidate_selection",
        selected_candidate_id=selected_candidate_id,
        candidate_count=evidence.candidate_count,
        selected_identity_in_candidate_set=True,
        selected_identity_was_read=True,
        reason_code="agent_selection_bound_to_verified_candidate_set",
        feedback="The agent-selected identity was returned by Gmail and read before triage.",
        repair_attempted=repair_attempted,
    )


_COMPLETED_EVENT_REQUEST_PATTERNS = (
    re.compile(
        r"\b(?:after|finished|completed|wrapped\s+up|already\s+had)\b"
        r".{0,80}\b(?:interview|meeting|conversation|call)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:interview|meeting|conversation|call)\b.{0,80}"
        r"\b(?:yesterday|earlier\s+today|already\s+happened|is\s+over|was\s+completed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bpost[- ](?:interview|meeting|conversation|call)\b", re.IGNORECASE),
)

_PRE_EVENT_REPLY_PATTERNS = (
    re.compile(
        r"\b(?:interview|meeting|conversation|call)\b.{0,60}"
        r"\b(?:is|has\s+been|was)?\s*(?:set|scheduled|confirmed)\b.{0,60}"
        r"\b(?:tomorrow|later\s+today|next\s+(?:week|monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:talk|meet|speak|chat)\b.{0,40}"
        r"\b(?:tomorrow|later\s+today|next\s+(?:week|monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday))\b",
        re.IGNORECASE,
    ),
)


def _gmail_reply_lifecycle_conflicts_with_request(
    *,
    original_request: str,
    result: EmailTriageResult,
) -> bool:
    """Reject an explicit past-event ask paired with clearly pre-event reply copy.

    This check validates the model's semantic decision without selecting another
    provider candidate. It is intentionally narrow: uncertain chronology remains
    the specialist's judgment, while an explicit lifecycle contradiction receives
    one evidence-preserving repair opportunity.
    """

    request_text = " ".join(str(original_request or "").split())
    if not request_text or not any(
        pattern.search(request_text) for pattern in _COMPLETED_EVENT_REQUEST_PATTERNS
    ):
        return False
    decision_text = " ".join(
        part
        for part in (
            str(result.draft_reply or ""),
            str(result.recommended_action or ""),
            str(result.reasoning or ""),
        )
        if part
    )
    return bool(
        decision_text
        and any(pattern.search(decision_text) for pattern in _PRE_EVENT_REPLY_PATTERNS)
    )


def validate_verified_gmail_continuation_decision(
    result: EmailTriageResult,
    raw_result: Any,
    *,
    expected_message_id: str = "",
    expected_thread_id: str = "",
    repair_attempted: bool = False,
) -> DecisionValidatorOutcome:
    """Validate a follow-up against one exact previously verified Gmail object."""

    evidence = gmail_decision_evidence(raw_result)
    output_message_id = str(result.message_id or "").strip()
    output_thread_id = str(result.thread_id or "").strip()
    selected_id = output_thread_id or output_message_id
    expected_selected_id = expected_thread_id or expected_message_id
    decision_id = str(result.decision.selected_candidate_id or "").strip()
    decision_explicit = _gmail_decision_was_explicitly_returned(result)
    owner_and_stage_match = bool(
        result.decision.decision_owner == "specialist_agent"
        and result.decision.decision_stage == "gmail_verified_continuation"
    )
    decision_shape_matches = bool(
        decision_explicit
        and not result.decision.needs_more_context
        and bool(str(result.decision.reasoning or "").strip())
        and result.decision.selected_candidate_ids == [expected_selected_id]
        and len(result.decision.candidate_assessments) == 1
        and result.decision.candidate_assessments[0].candidate_id == expected_selected_id
        and result.decision.candidate_assessments[0].disposition == "selected"
    )
    execution_shape_matches = bool(
        evidence.query_call_count == 0
        and evidence.context_read_call_count == 1
        and evidence.context_read_output_count == 1
    )
    identity_matches = bool(
        (not expected_message_id or output_message_id == expected_message_id)
        and (not expected_thread_id or output_thread_id == expected_thread_id)
    )
    read_matches = bool(
        (expected_message_id and expected_message_id in evidence.read_message_ids)
        or (expected_thread_id and expected_thread_id in evidence.read_thread_ids)
    )
    unrelated_read = bool(
        set(evidence.read_message_ids).difference({expected_message_id})
        or set(evidence.read_thread_ids).difference({expected_thread_id})
    )
    decision_matches = bool(decision_id and decision_id == expected_selected_id)
    status = (
        "accepted"
        if (
            identity_matches
            and read_matches
            and not unrelated_read
            and decision_matches
            and decision_shape_matches
            and owner_and_stage_match
            and execution_shape_matches
        )
        else "repair_required"
    )
    return DecisionValidatorOutcome(
        status=status,
        decision_stage="gmail_verified_continuation",
        selected_candidate_id=selected_id,
        candidate_count=1,
        selected_identity_in_candidate_set=identity_matches,
        selected_identity_was_read=read_matches,
        reason_code=(
            "verified_gmail_continuation_read_and_preserved"
            if status == "accepted"
            else "verified_gmail_continuation_mismatch"
        ),
        feedback=(
            "The exact verified Gmail continuation was read and preserved."
            if status == "accepted"
            else (
                "Use decision_owner=specialist_agent and "
                "decision_stage=gmail_verified_continuation; make no Gmail query, "
                "read only the exact verified object once, and return its exact identity."
            )
        ),
        repair_attempted=repair_attempted,
    )


def decision_record_from_gmail_result(result: EmailTriageResult) -> AgentDecisionRecord:
    """Return the model's decision unchanged; never synthesize a Gmail choice."""

    return result.decision


def _gmail_decision_was_explicitly_returned(result: EmailTriageResult) -> bool:
    """Reject both omitted Pydantic defaults and their compatibility-copy shape."""

    if "decision" not in set(result.model_fields_set):
        return False
    decision = result.decision
    return not bool(
        decision.decision_owner == "specialist_agent"
        and decision.decision_stage == "specialist_selection"
        and not decision.selected_candidate_ids
        and not decision.candidate_assessments
        and not decision.reasoning
        and not decision.limitations
        and not decision.needs_more_context
    )


def _claimed_gmail_output_fields(result: EmailTriageResult) -> tuple[str, ...]:
    """Return provider-specific/action-bearing claims incompatible with indecision."""

    claimed: list[str] = []
    scalar_fields = (
        "message_id",
        "thread_id",
        "received_at",
        "subject",
        "sender_name",
        "sender_email",
        "thread_summary",
        "thread_context",
        "snippet",
        "normalized_body",
        "draft_reply",
        "style_profile_id",
    )
    for field_name in scalar_fields:
        if str(getattr(result, field_name, "") or "").strip():
            claimed.append(field_name)
    list_fields = (
        "recommended_labels",
        "risk_flags",
        "suspicious_signals",
        "prior_labels",
        "extracted_links",
        "attachment_metadata",
    )
    for field_name in list_fields:
        if list(getattr(result, field_name, []) or []):
            claimed.append(field_name)
    if result.needs_reply:
        claimed.append("needs_reply")
    if result.draft_created:
        claimed.append("draft_created")
    if result.style_profile_used:
        claimed.append("style_profile_used")
    return tuple(claimed)


def gmail_claimed_output_fields(result: EmailTriageResult) -> tuple[str, ...]:
    """Expose the exact provider-specific claims that conflict with abstention."""

    return _claimed_gmail_output_fields(result)


def normalize_empty_gmail_abstention(
    result: EmailTriageResult,
    evidence: GmailDecisionEvidence,
) -> tuple[EmailTriageResult, tuple[str, ...]]:
    """Discard ungrounded context slots from an explicit, verified empty-search abstention.

    These strings are never interpreted or promoted as evidence. Identity or
    action claims still require the usual model repair; this only avoids a paid
    repair whose sole purpose would be clearing unused evidence fields.
    """
    discardable = {"normalized_body", "thread_context", "thread_summary"}
    claimed = _claimed_gmail_output_fields(result)
    decision = result.decision
    if not (
        _gmail_decision_was_explicitly_returned(result)
        and "needs_more_context" in decision.model_fields_set
        and decision.needs_more_context
        and not decision.selected_candidate_id
        and not decision.selected_candidate_ids
        and not decision.candidate_assessments
        and result.recommended_next_agent in {"", "human_review"}
        and evidence.query_provider_read_performed
        and evidence.query_output_count > 0
        and len(evidence.query_attempts) == evidence.query_output_count
        and all(item.get("provider_read_performed") is True
                and item.get("returned_candidate_count") == 0
                for item in evidence.query_attempts)
        and not evidence.candidate_summaries
        and not evidence.candidate_message_ids
        and not evidence.candidate_thread_ids
        and not evidence.context_read_call_count
        and not evidence.read_context_summaries
        and not evidence.read_message_ids
        and not evidence.read_thread_ids
        and claimed
        and set(claimed) <= discardable
    ):
        return result, ()
    return result.model_copy(update=dict.fromkeys(claimed, "")), claimed


def gmail_decision_telemetry(
    result: EmailTriageResult,
    outcome: DecisionValidatorOutcome,
    evidence: GmailDecisionEvidence,
) -> dict[str, Any]:
    """Build one terminal telemetry block for successful or rejected selection."""

    excluded_candidate_ids = list(
        dict.fromkeys(
            evidence.canonical_candidate_id(item.candidate_id)
            for item in result.decision.candidate_assessments
            if item.disposition == "excluded"
        )
    )
    selected_candidate_ids = list(
        dict.fromkeys(
            evidence.canonical_candidate_id(candidate_id)
            for candidate_id in result.decision.selected_candidate_ids
        )
    )
    attempt = 2 if outcome.repair_attempted else 1
    decision_stage = outcome.decision_stage
    tool_mode = "verified_context_tool_free" if outcome.repair_attempted else "model_called"
    terminal_status = "accepted" if outcome.status == "accepted" else "rejected"
    events = [
        DecisionTelemetryEvent(
            event_type="proposed",
            decision_owner=result.decision.decision_owner,
            decision_stage=decision_stage,
            attempt=attempt,
            candidate_ids=list(evidence.decision_candidate_ids),
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            tool_mode=tool_mode,
        ),
        DecisionTelemetryEvent(
            event_type="validator_result",
            decision_owner=result.decision.decision_owner,
            decision_stage=decision_stage,
            attempt=attempt,
            candidate_ids=list(evidence.decision_candidate_ids),
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            validator_status=outcome.status,
            reason_code=outcome.reason_code,
            tool_mode=tool_mode,
        ),
        DecisionTelemetryEvent(
            event_type="terminal",
            decision_owner=result.decision.decision_owner,
            decision_stage=decision_stage,
            attempt=attempt,
            candidate_ids=list(evidence.decision_candidate_ids),
            selected_candidate_ids=selected_candidate_ids,
            excluded_candidate_ids=excluded_candidate_ids,
            validator_status=terminal_status,
            reason_code=outcome.reason_code,
            tool_mode=tool_mode,
        ),
    ]
    return {
        "schema": "keystone.decision_ownership_telemetry.v1",
        "decision_owner": result.decision.decision_owner,
        "decision_stage": decision_stage,
        "candidate_count": evidence.candidate_count,
        "query_message_count": evidence.candidate_count,
        "query_provider_read_performed": evidence.query_provider_read_performed,
        "query_thread_count": evidence.query_thread_count,
        "decision_candidate_count": len(evidence.decision_candidate_ids),
        "query_call_count": evidence.query_call_count,
        "query_output_count": evidence.query_output_count,
        "corrective_query_count": evidence.corrective_query_count,
        "model_query_call_count": evidence.model_query_call_count,
        "repeated_query_call_count": evidence.repeated_query_call_count,
        "blocked_query_call_count": evidence.blocked_query_call_count,
        "query_attempts": [dict(item) for item in evidence.query_attempts],
        "context_read_call_count": evidence.context_read_call_count,
        "context_read_output_count": evidence.context_read_output_count,
        "context_provider_read_count": evidence.context_provider_read_count,
        "model_context_read_call_count": evidence.model_context_read_call_count,
        "repeated_context_read_call_count": evidence.repeated_context_read_call_count,
        "blocked_context_read_call_count": evidence.blocked_context_read_call_count,
        "rejected_context_read_call_count": evidence.rejected_context_read_call_count,
        "candidate_ids": list(evidence.candidate_thread_ids),
        "decision_candidate_ids": list(evidence.decision_candidate_ids),
        "selected_candidate_id": evidence.canonical_candidate_id(
            result.decision.selected_candidate_id
        ),
        "excluded_candidate_ids": excluded_candidate_ids,
        "reasoning": result.decision.reasoning,
        "limitations": list(result.decision.limitations),
        "needs_more_context": result.decision.needs_more_context,
        "validator_outcome": outcome.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
    }


def _repair_outcome(
    reason_code: str,
    feedback: str,
    evidence: GmailDecisionEvidence,
    *,
    selected_candidate_id: str = "",
    selected_identity_in_candidate_set: bool | None = None,
    selected_identity_was_read: bool | None = None,
    repair_attempted: bool,
) -> DecisionValidatorOutcome:
    return DecisionValidatorOutcome(
        status="rejected" if repair_attempted else "repair_required",
        decision_stage="gmail_candidate_selection",
        selected_candidate_id=selected_candidate_id,
        candidate_count=evidence.candidate_count,
        selected_identity_in_candidate_set=selected_identity_in_candidate_set,
        selected_identity_was_read=selected_identity_was_read,
        reason_code=reason_code,
        feedback=feedback,
        repair_attempted=repair_attempted,
    )


__all__ = [
    "GmailDecisionEvidence",
    "decision_record_from_gmail_result",
    "gmail_claimed_output_fields",
    "gmail_decision_evidence",
    "gmail_decision_telemetry",
    "normalize_empty_gmail_abstention",
    "validate_gmail_agent_decision",
    "validate_verified_gmail_continuation_decision",
]
