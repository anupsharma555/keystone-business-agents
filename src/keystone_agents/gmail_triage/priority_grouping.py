"""Shared provider-first Gmail collection triage workflow."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from keystone_agents.agents.gmail_triage import (
    build_gmail_priority_grouping_agent,
    run_gmail_candidate_ranking_sdk,
)
from keystone_agents.gmail_triage.execution_plan import gmail_provider_read_scope
from keystone_agents.models import (
    GmailCandidateRankingSDKInput,
    GmailPriorityGroupingSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.run import SDKSynthesisOutcome, run_retrieved_sdk_synthesis
from keystone_agents.schemas.email_triage import (
    GmailCandidateRankingItem,
    GmailCandidateRankingResult,
    GmailPriorityGroupedMessage,
    GmailPriorityGroupingResult,
    GmailThreadSummaryResult,
)
from keystone_agents.schemas.gmail_execution_plan import GmailExecutionPlan
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.semantic_execution import (
    StageOperationBoundary,
    StageOutputContract,
    reconcile_stage_output,
)
from keystone_agents.tools.gmail_tool import (
    GmailTool,
    gmail_message_envelope_from_dict,
)


@dataclass(frozen=True)
class GmailPriorityGroupingExecution:
    """One verified provider read plus one bounded specialist synthesis."""

    outcome: SDKSynthesisOutcome
    result: GmailPriorityGroupingResult
    provider_receipt: dict[str, Any]
    human_summary: str


@dataclass(frozen=True)
class GmailSemanticCandidateRanking:
    """Model-ranked Gmail candidates bound to the supplied provider identities."""

    outcome: TypedAgentRunResult[GmailCandidateRankingResult]
    result: GmailCandidateRankingResult
    ranked_thread_ids: tuple[str, ...]
    candidate_payloads: tuple[dict[str, Any], ...]


def rank_gmail_candidates_for_request(
    *,
    operator_request: str,
    summaries: list[GmailThreadSummaryResult],
    live_sdk: bool,
    model: str | None = None,
) -> GmailSemanticCandidateRanking:
    """Use Gmail-agent reasoning to rank one bounded provider result set.

    Provider retrieval and identity remain deterministic. The Gmail specialist
    receives the authoritative current ask plus sanitized candidate evidence,
    and its structured classification supplies the semantic ranking.
    """

    candidate_payloads = tuple(
        _thread_summary_candidate_payload(summary) for summary in summaries
    )
    typed_input = GmailCandidateRankingSDKInput.from_envelopes(
        [
            gmail_message_envelope_from_dict(payload)
            for payload in candidate_payloads
        ],
        operator_request=operator_request,
        source_label="BOUNDED_PROVIDER_RESULT",
    )
    outcome = run_gmail_candidate_ranking_sdk(
        typed_input,
        live=live_sdk,
        model=model,
    )
    provider_thread_by_message = {
        str(payload.get("id") or "").strip(): str(payload.get("threadId") or "").strip()
        for payload in candidate_payloads
        if str(payload.get("id") or "").strip()
    }
    result = outcome.final_output
    unknown_ids = {
        item.message_id for item in result.candidates
    } - set(provider_thread_by_message)
    if unknown_ids:
        raise RuntimeError(
            "Gmail candidate ranking cited a message outside the provider result set."
        )
    returned_ids = {item.message_id for item in result.candidates}
    missing_ids = [
        message_id
        for message_id in provider_thread_by_message
        if message_id not in returned_ids
    ]
    if missing_ids:
        result = result.model_copy(
            update={
                "candidates": [
                    *result.candidates,
                    *[
                        GmailCandidateRankingItem(
                            message_id=message_id,
                            disposition="manual_review",
                            relevance_score=0.0,
                            needs_reply=False,
                            reasoning=(
                                "The specialist omitted this provider candidate, so it "
                                "was retained for manual review and not selected."
                            ),
                        )
                        for message_id in missing_ids
                    ],
                ],
                "audit_notes": list(
                    dict.fromkeys(
                        [
                            *result.audit_notes,
                            f"Coverage repair retained {len(missing_ids)} omitted "
                            "provider candidate(s) for manual review.",
                        ]
                    )
                ),
            }
        )
    ranked = sorted(
        [
            item
            for item in result.candidates
            if item.disposition == "candidate"
        ],
        key=lambda item: (item.needs_reply, item.relevance_score),
        reverse=True,
    )
    return GmailSemanticCandidateRanking(
        outcome=outcome,
        result=result,
        ranked_thread_ids=tuple(
            dict.fromkeys(
                provider_thread_by_message[item.message_id]
                for item in ranked
                if provider_thread_by_message[item.message_id]
            )
        ),
        candidate_payloads=candidate_payloads,
    )


def _thread_summary_candidate_payload(
    summary: GmailThreadSummaryResult,
) -> dict[str, Any]:
    """Project one sanitized thread summary into the Gmail SDK input contract."""

    latest = summary.messages[-1] if summary.messages else None
    sender_name = latest.sender_name if latest is not None else ""
    sender_email = latest.sender_email if latest is not None else ""
    from_header = (
        f"{sender_name} <{sender_email}>"
        if sender_name and sender_email
        else sender_email or sender_name
    )
    message_id = (
        latest.message_id
        if latest is not None and latest.message_id
        else summary.thread_id
    )
    snippet = (
        latest.snippet
        if latest is not None and latest.snippet
        else summary.summary or summary.thread_context
    )
    prior_labels = latest.prior_labels if latest is not None else []
    return {
        "id": message_id,
        "threadId": summary.thread_id,
        "received_at": summary.latest_received_at,
        "from": from_header,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "subject": summary.subject,
        "snippet": snippet,
        "prior_labels": prior_labels,
        "thread_summary": summary.summary,
        "thread_context": summary.thread_context,
        "thread_message_count": summary.message_count,
        "triage_limitations": [
            "Candidate contains sanitized thread evidence only; raw Gmail body was not supplied."
        ],
    }


def run_gmail_priority_grouping_workflow(
    *,
    operator_request: str,
    gmail_plan: GmailExecutionPlan,
    gmail_tool: GmailTool | None = None,
    run_config: Any | None = None,
    live_sdk: bool = False,
    model: str | None = None,
) -> GmailPriorityGroupingExecution:
    """Read one complete bounded Gmail collection, then classify it once."""

    if gmail_plan.operation != "priority_grouping":
        raise ValueError("Gmail priority workflow requires operation=priority_grouping.")
    if gmail_plan.side_effect_policy != "read_only":
        raise ValueError("Gmail priority workflow requires a read-only execution plan.")
    if gmail_plan.draft_replies_in_output:
        raise ValueError("Gmail priority workflow cannot include draft replies for this request.")
    gmail = gmail_tool or GmailTool(live=True)
    label = str(gmail_plan.source_label or "").strip() or None
    query = str(gmail_provider_read_scope(gmail_plan).get("query") or "").strip()
    provider_state: dict[str, Any] = {}

    def retrieve() -> list[dict[str, Any]]:
        count_receipt = gmail.count_messages(label=label, query=query)
        if count_receipt.get("complete") is not True:
            raise RuntimeError("The bounded Gmail collection count was incomplete.")
        candidate_count = int(count_receipt.get("message_count") or 0)
        if candidate_count > gmail_plan.max_messages:
            raise RuntimeError(
                "The Gmail result set exceeds the bounded collection limit; narrow the request."
            )
        quarantine_applied = False
        try:
            summaries = gmail.search_message_summaries(
                label=label,
                query=query,
                max_results=max(1, candidate_count),
            )
        except ToolGuardrailViolation:
            quarantine_applied = True
            refs = gmail.list_recent_messages(
                label=label,
                query=query,
                max_results=max(1, candidate_count),
            )
            message_ids = [
                str(ref.get("id") or "").strip()
                for ref in refs
                if str(ref.get("id") or "").strip()
            ]
            summaries = gmail.batch_get_messages(message_ids, skip_blocked=True)
        if not quarantine_applied and len(summaries) != candidate_count:
            raise RuntimeError(
                "The Gmail collection changed between the exact count and message read."
            )
        if quarantine_applied and candidate_count and not summaries:
            raise RuntimeError(
                "Every Gmail message in the bounded collection was withheld by safety guardrails."
            )
        provider_state["candidate_count"] = candidate_count
        provider_state["quarantined_message_count"] = max(
            0,
            candidate_count - len(summaries),
        )
        provider_state["guardrail_quarantine_applied"] = quarantine_applied
        return summaries

    def normalize(summaries: list[dict[str, Any]]) -> GmailPriorityGroupingSDKInput:
        envelopes = [gmail_message_envelope_from_dict(summary) for summary in summaries]
        typed_input = GmailPriorityGroupingSDKInput.from_envelopes(
            envelopes,
            request=operator_request,
            operator_request=operator_request,
            lookback_days=gmail_plan.lookback_days,
            source_label=str(label or "ALL"),
        )
        return replace(
            typed_input,
            draft_policy=(
                "Do not draft replies or create provider drafts for any message. "
                "Classify and summarize only."
            ),
        )

    def finalize(
        summaries: list[dict[str, Any]],
        result: GmailPriorityGroupingResult,
    ) -> GmailPriorityGroupingResult:
        authoritative = result.model_copy(
            update={
                "request_summary": operator_request,
                "source_label": str(label or "ALL"),
                "lookback_days": gmail_plan.lookback_days,
                "source_message_count": len(summaries),
            }
        )
        return _bind_priority_result_to_provider(
            authoritative,
            summaries=summaries,
            coverage_state=provider_state,
        )

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_priority_grouping_agent(
            model=model,
            request_text=operator_request,
        ),
        output_type=GmailPriorityGroupingResult,
        retrieve=retrieve,
        normalize=normalize,
        finalize_output=finalize,
        input_summary="bounded read-only Gmail collection triage",
        input_audit_payload={
            "operation": "priority_grouping",
            "query": query,
            "label": str(label or ""),
            "max_messages": gmail_plan.max_messages,
            "provider_write": False,
        },
        workflow_name="Keystone Gmail Priority Grouping",
        trace_metadata={
            "workflow_kind": "gmail_priority_grouping",
            "provider": "gmail",
            "provider_write": False,
        },
        run_config=run_config,
        live=live_sdk,
        save=False,
        model_label="sdk-live" if live_sdk else "sdk-local",
    )
    result = outcome.final_output
    summaries = outcome.raw_context
    candidate_count = int(provider_state.get("candidate_count") or 0)
    quarantined_message_count = int(
        provider_state.get("quarantined_message_count") or 0
    )
    receipt = {
        "provider": "gmail",
        "operation": "bounded_priority_grouping_read",
        "query": query,
        "label": str(label or ""),
        "candidate_count": candidate_count,
        "admitted_message_count": len(summaries),
        "quarantined_message_count": quarantined_message_count,
        "guardrail_quarantine_applied": bool(
            provider_state.get("guardrail_quarantine_applied")
        ),
        "classification_repair_count": int(
            provider_state.get("classification_repair_count") or 0
        ),
        "discarded_plaintext_draft_count": int(
            provider_state.get("discarded_plaintext_draft_count") or 0
        ),
        "selected_message_ids": [
            str(summary.get("id") or "").strip()
            for summary in summaries
            if str(summary.get("id") or "").strip()
        ],
        "complete": True,
        "verified": True,
        "provider_read": True,
        "provider_write": False,
    }
    human_summary = gmail_priority_grouping_human_summary(result)
    if quarantined_message_count:
        human_summary = (
            f"{human_summary}\n\n"
            f"Safety note: {quarantined_message_count} message(s) were withheld "
            "from classification because their content triggered a safety guardrail."
        )
    return GmailPriorityGroupingExecution(
        outcome=outcome,
        result=result,
        provider_receipt=receipt,
        human_summary=human_summary,
    )


def _bind_priority_result_to_provider(
    result: GmailPriorityGroupingResult,
    *,
    summaries: list[dict[str, Any]],
    coverage_state: dict[str, Any] | None = None,
) -> GmailPriorityGroupingResult:
    """Require complete one-to-one provider coverage and bind displayed identity."""

    discarded_plaintext_draft_count = max(
        int(result.draft_count or 0),
        sum(
            1
            for bucket in ("urgent", "important", "can_wait", "ignore")
            for item in getattr(result, bucket)
            if item.draft_reply
        ),
    )
    stage_output = reconcile_stage_output(
        result,
        contract=StageOutputContract(
            stage="gmail_candidate_ranking",
            operation_boundary=StageOperationBoundary.READ_ONLY,
            discard_fields=frozenset({"draft_reply", "draft_count"}),
            public_output=False,
        ),
    )
    if not stage_output.safe:
        raise RuntimeError(
            "Gmail priority grouping claimed a provider mutation during a read-only "
            "ranking phase: "
            + ", ".join(stage_output.prohibited_effect_paths)
        )
    result = GmailPriorityGroupingResult.model_validate(stage_output.payload)

    provider_by_id: dict[str, Any] = {}
    for summary in summaries:
        message_id = str(summary.get("id") or "").strip()
        if message_id:
            provider_by_id[message_id] = gmail_message_envelope_from_dict(summary)
    classified = [
        item
        for bucket in ("urgent", "important", "can_wait", "ignore")
        for item in getattr(result, bucket)
    ]
    classified_ids = [str(item.message_id or "").strip() for item in classified]
    if len(classified_ids) != len(set(classified_ids)):
        raise RuntimeError(
            "Gmail priority grouping classified one provider message more than once."
        )
    unknown_ids = set(classified_ids) - set(provider_by_id)
    if unknown_ids:
        raise RuntimeError(
            "Gmail priority grouping cited a message outside the provider result set."
        )
    updates: dict[str, Any] = {}
    for bucket in ("urgent", "important", "can_wait", "ignore"):
        bound_items: list[GmailPriorityGroupedMessage] = []
        for item in getattr(result, bucket):
            envelope = provider_by_id[item.message_id]
            bound_items.append(
                item.model_copy(
                    update={
                        "thread_id": envelope.thread_id,
                        "received_at": envelope.received_at,
                        "subject": envelope.subject or "(no subject)",
                        "sender_name": envelope.sender_name,
                        "sender_email": envelope.sender_email,
                        "draft_reply": None,
                        "draft_created": False,
                        "send_enabled": False,
                        "sent": False,
                    }
                )
            )
        updates[bucket] = bound_items
    missing_ids = [
        message_id
        for message_id in provider_by_id
        if message_id not in set(classified_ids)
    ]
    for message_id in missing_ids:
        envelope = provider_by_id[message_id]
        updates["important"].append(
            GmailPriorityGroupedMessage(
                message_id=message_id,
                thread_id=envelope.thread_id,
                received_at=envelope.received_at,
                subject=envelope.subject or "(no subject)",
                sender_name=envelope.sender_name,
                sender_email=envelope.sender_email,
                bucket="important",
                category="unrelated",
                confidence=0.0,
                priority="normal",
                summary=(
                    envelope.snippet
                    or "The specialist did not return a classification for this message."
                ),
                reasoning=(
                    "The specialist omitted this provider message, so the validator "
                    "surfaced it conservatively for manual review."
                ),
                needs_reply=False,
                recommended_action="Review manually; no action was inferred.",
                requires_human_review=True,
            )
        )
    updates["draft_count"] = 0
    updates["audit_notes"] = list(
        dict.fromkeys(
            [
                *result.audit_notes,
                *(
                    [
                        f"Coverage repair surfaced {len(missing_ids)} omitted provider "
                        "message(s) for manual review."
                    ]
                    if missing_ids
                    else []
                ),
                *(
                    [
                        "Discarded "
                        f"{discarded_plaintext_draft_count} unrequested plain-text "
                        "draft(s) from the read-only candidate-ranking phase."
                    ]
                    if discarded_plaintext_draft_count
                    else []
                ),
            ]
        )
    )
    if coverage_state is not None:
        coverage_state["classification_repair_count"] = len(missing_ids)
        coverage_state["discarded_plaintext_draft_count"] = (
            discarded_plaintext_draft_count
        )
    return result.model_copy(update=updates)


def gmail_priority_grouping_human_summary(result: GmailPriorityGroupingResult) -> str:
    """Render one compact operator-facing triage answer from bound result fields."""

    lines: list[str] = []
    attention = [*result.urgent, *result.important]
    lines.append("Needs your attention:")
    if attention:
        lines.extend(_priority_item_line(item) for item in attention)
    else:
        lines.append("- None of today's messages were classified as urgent or important.")
    lines.append("")
    lines.append("Can wait:")
    if result.can_wait:
        lines.extend(_priority_item_line(item) for item in result.can_wait)
    else:
        lines.append("- None.")
    if result.ignore:
        lines.extend(["", f"Low-priority/ignore: {len(result.ignore)} message(s)."])
    return "\n".join(lines).strip()


def _priority_item_line(item: GmailPriorityGroupedMessage) -> str:
    sender = item.sender_name or item.sender_email or "Unknown sender"
    action = item.recommended_action or item.summary or item.reasoning
    return f"- {item.subject or '(no subject)'} - {sender}: {action}"
