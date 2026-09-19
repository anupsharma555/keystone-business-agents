"""Gmail triage agent builder and deterministic fixture-mode triage."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from keystone_agents.capabilities.tool_scope import (
    ToolScopeMode,
    attach_tool_scope_receipt,
    default_tool_tier_for_request,
    scope_tools_for_request,
    tool_scope_receipt_for_agent,
    tool_scope_trace_metadata_for_agent,
)
from keystone_agents.gmail_triage.decision_ownership import (
    decision_record_from_gmail_result,
    gmail_claimed_output_fields,
    gmail_decision_evidence,
    gmail_decision_telemetry,
    normalize_empty_gmail_abstention,
    validate_gmail_agent_decision,
    validate_verified_gmail_continuation_decision,
)
from keystone_agents.gmail_triage.handoff_context import selected_provider_context
from keystone_agents.gmail_triage.text import (
    clean_text as _clean_text,
)
from keystone_agents.gmail_triage.text import (
    contains_any as _contains_any,
)
from keystone_agents.gmail_triage.text import (
    normalize_body_for_triage as _normalize_body_for_triage,
)
from keystone_agents.gmail_triage.text import (
    style_cta as _style_cta,
)
from keystone_agents.gmail_triage.text import (
    style_greeting as _style_greeting,
)
from keystone_agents.gmail_triage.text import (
    style_signoff as _style_signoff,
)
from keystone_agents.guardrails import (
    acknowledgement_only_reply,
    assess_text_guardrails,
    keystone_guardrails,
)
from keystone_agents.model_provider import gmail_selection_reasoning_effort
from keystone_agents.models import (
    GmailCandidateRankingSDKInput,
    GmailContactLookupSDKInput,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.request_budget import current_model_request_capacity
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
    ToolExecutionMode,
)
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.email_triage import (
    EmailTriageResult,
    GmailCandidateRankingResult,
    GmailContactLookupResult,
    GmailMailboxActionPlan,
    GmailMessageEnvelope,
    GmailNeedsMoreContextRepair,
    GmailPriorityGroupedMessage,
    GmailPriorityGroupingResult,
    GmailSelectedSelectionRepair,
    GmailSelectionRepairResult,
    managed_gmail_labels,
)
from keystone_agents.schemas.gmail_query import GmailReadContextResult
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import (
    Agent,
    build_model_settings,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
from keystone_agents.sdk_run_policy import resolve_sdk_turn_policy
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text
from keystone_agents.tools.approval_tool import create_approval_queue_item
from keystone_agents.tools.email_style_tool import load_email_style_profile
from keystone_agents.tools.gmail_query_tools import (
    gmail_model_read_evidence_snapshot,
    gmail_model_read_tools,
)
from keystone_agents.tools.gmail_tool import (
    apply_gmail_labels,
    create_gmail_draft_reply,
    create_gmail_draft_with_attachment,
    get_gmail_message,
    gmail_message_envelope_from_dict,
    gmail_test_draft_lifecycle,
    modify_gmail_message_state,
    send_gmail_test_draft,
)
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
    airtable_write_record,
    google_workspace_tools,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import retrieve_memory
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.storage_tool import list_outreach_tracking_records
from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema


@dataclass(frozen=True)
class EmailFixture:
    subject: str
    body: str
    sender_name: str = ""
    sender_email: str = ""
    message_id: str = "fixture-message"
    thread_id: str = "fixture-thread"
    received_at: str = ""
    snippet: str = ""
    prior_labels: tuple[str, ...] = ()


def _coerce_style_profile(
    value: EmailStyleProfile | dict[str, Any] | None,
) -> EmailStyleProfile | None:
    if value is None:
        return None
    if isinstance(value, EmailStyleProfile):
        return value
    return EmailStyleProfile.model_validate(value)


def load_email_fixture(
    fixture: str | Path | None,
    *,
    subject: str = "",
    sender_name: str = "",
    sender_email: str = "",
) -> EmailFixture:
    """Load a deterministic local email fixture from a plain-text file."""

    text = Path(fixture).read_text(encoding="utf-8") if fixture else ""
    lines = text.splitlines()
    parsed_subject = subject.strip()
    body_lines = lines
    if not parsed_subject and lines and lines[0].lower().startswith("subject:"):
        parsed_subject = lines[0].split(":", 1)[1].strip()
        body_lines = lines[1:]

    parsed_name = sender_name.strip()
    parsed_email = sender_email.strip()
    if parsed_email and not parsed_name:
        parsed_name = parseaddr(parsed_email)[0]

    return EmailFixture(
        subject=_clean_text(parsed_subject or "(no subject)"),
        body=_clean_text("\n".join(body_lines).strip()),
        sender_name=_clean_text(parsed_name),
        sender_email=_clean_text(parseaddr(parsed_email)[1] or parsed_email),
    )


def email_fixture_to_envelope(email: EmailFixture) -> GmailMessageEnvelope:
    """Normalize fixture email through the live Gmail envelope shape."""

    return gmail_message_envelope_from_dict(
        {
            "id": email.message_id,
            "threadId": email.thread_id,
            "received_at": email.received_at,
            "from": f"{email.sender_name} <{email.sender_email}>",
            "subject": email.subject,
            "snippet": email.snippet,
            "labelIds": list(email.prior_labels),
            "body": email.body,
            "triage_limitations": ["Fixture mode uses deterministic local classification rules."],
        }
    )


def _base_labels(category: str, needs_reply: bool, risk_flags: list[str]) -> list[str]:
    return managed_gmail_labels(
        category=category,  # type: ignore[arg-type]
        needs_reply=needs_reply,
        risk_flags=risk_flags,
        approval_required=needs_reply,
    )


def _recommended_next_agent(category: str, risk_flags: list[str], needs_reply: bool) -> str:
    if (
        "security" in risk_flags
        or "possible_phi" in risk_flags
        or "professional_advice" in risk_flags
    ):
        return "human_review"
    if "legal_review" in risk_flags or "legal" in risk_flags:
        return "legal_review"
    if "finance_review" in risk_flags or "finance" in risk_flags:
        return "finance_review"
    if category in {"consulting_opportunity", "collaboration_opportunity"}:
        return "business_research_analyst"
    if needs_reply:
        return "human_review"
    return "none"


def _draft_reply(
    email: EmailFixture,
    category: str,
    style_profile: EmailStyleProfile | dict[str, Any] | None = None,
) -> str:
    profile = _coerce_style_profile(style_profile)
    if profile is not None and not profile.approved_for_drafting:
        profile = None
    greeting = _style_greeting(email.sender_name, profile)
    if category == "consulting_opportunity":
        cta = _style_cta(
            (
                "Please send any non-sensitive project context and a few times that work "
                "for your team."
            ),
            profile,
        )
        body = (
            "Thanks for reaching out about the clinical operations workflow. "
            "Keystone can review whether there is a practical fit for a short advisory discussion. "
            f"{cta}"
        )
    else:
        cta = _style_cta(
            "Please send a short overview of the pilot goals, timeline, and input you are seeking.",
            profile,
        )
        body = (
            "Thanks for reaching out. The collaboration sounds relevant to Keystone's focus areas. "
            f"{cta}"
        )
    return f"{greeting}\n\n{body}\n\n{_style_signoff(profile)}"


def triage_email_fixture(
    email: EmailFixture,
    *,
    email_style_profile: EmailStyleProfile | dict[str, Any] | None = None,
) -> EmailTriageResult:
    """Classify one email deterministically without calling the LLM or Gmail APIs."""

    style_profile = _coerce_style_profile(email_style_profile)
    style_profile_used = bool(style_profile and style_profile.approved_for_drafting)
    if style_profile is not None and not style_profile.approved_for_drafting:
        style_profile = None
    envelope = email_fixture_to_envelope(email)
    triage_body = _normalize_body_for_triage(envelope.normalized_body or email.body)
    link_text = "\n".join(link.url for link in envelope.extracted_links)
    combined_raw = f"{email.subject}\n{triage_body}\n{email.sender_email}\n{link_text}"
    combined = combined_raw.lower()
    assessment = assess_text_guardrails(combined_raw, check_outreach_claims=False)
    risk_flags = list(assessment.risk_flags)
    suspicious_signals = list(envelope.suspicious_signals)
    if any(
        "credential" in signal.lower() or "shortened" in signal.lower()
        for signal in suspicious_signals
    ):
        risk_flags.append("security")
    risk_flags = list(dict.fromkeys(risk_flags))

    if "security" in risk_flags or "possible_phi" in risk_flags:
        category = "suspicious"
        needs_reply = False
        priority = "urgent" if "security" in risk_flags else "high"
        confidence = 0.93
        summary = (
            "Message requires manual review because it contains security or PHI risk indicators."
        )
        action = "Do not reply. Route to manual review."
    elif "legal_review" in risk_flags:
        category = "unrelated"
        needs_reply = True
        priority = "high"
        confidence = 0.9
        summary = "Legal or contract content requires review before any substantive response."
        action = "Acknowledge receipt only and route to legal review."
    elif "finance_review" in risk_flags:
        category = "unrelated"
        needs_reply = False
        priority = "normal"
        confidence = 0.86
        summary = "Financial content requires manual finance review before any response."
        action = "Route to finance review before responding."
    elif _contains_any(combined, ("newsletter", "digest", "webinar", "roundup")):
        category = "newsletter"
        needs_reply = False
        priority = "low"
        confidence = 0.88
        summary = "Informational newsletter or digest with no direct response request."
        action = "Archive or review later; no reply needed."
    elif _contains_any(combined, ("vendor", "platform", "book a demo", "qualified leads", "sales")):
        category = "vendor"
        needs_reply = False
        priority = "low"
        confidence = 0.84
        summary = "Vendor pitch that is not high-value by default."
        action = "Do not prioritize; review only if vendor evaluation is active."
    elif _contains_any(combined, ("collaboration", "collaborate", "pilot", "research partners")):
        category = "collaboration_opportunity"
        needs_reply = True
        priority = "high"
        confidence = 0.86
        summary = "Collaboration inquiry aligned with Keystone focus areas."
        action = "Create a draft reply for human approval."
    elif _contains_any(
        combined, ("consulting", "advisory", "clinical operations", "external consulting")
    ):
        category = "consulting_opportunity"
        needs_reply = True
        priority = "high"
        confidence = 0.89
        summary = "Consulting inquiry that appears relevant to Keystone services."
        action = "Create a draft reply for human approval."
    else:
        category = "unrelated"
        needs_reply = False
        priority = "normal"
        confidence = 0.65
        summary = "No clear Keystone business opportunity or required response was detected."
        action = "Review manually if context suggests importance."

    labels = _base_labels(category, needs_reply, risk_flags)
    if assessment.draft_policy == "acknowledge_only":
        draft = acknowledgement_only_reply(email.sender_name)
    elif needs_reply and not risk_flags:
        draft = _draft_reply(email, category, style_profile)
    else:
        draft = None
    limitations = list(envelope.triage_limitations)
    if envelope.attachment_metadata:
        limitations.append("Attachments were screened by metadata only and not ingested.")
    return EmailTriageResult(
        message_id=email.message_id,
        thread_id=email.thread_id,
        received_at=email.received_at,
        subject=email.subject,
        sender_name=email.sender_name,
        sender_email=email.sender_email,
        category=category,
        confidence=confidence,
        priority=priority,
        summary=summary,
        thread_summary=envelope.thread_summary,
        thread_context=envelope.thread_context,
        reasoning=(
            "Deterministic fixture-mode rules and guardrails classified the message from "
            "sanitized subject, normalized body, sender, links, and attachment metadata."
        ),
        needs_reply=needs_reply,
        recommended_labels=labels,
        risk_flags=risk_flags,
        suspicious_signals=list(dict.fromkeys([*suspicious_signals, *assessment.reasons])),
        recommended_next_agent=_recommended_next_agent(category, risk_flags, needs_reply),
        triage_limitations=list(dict.fromkeys(limitations)),
        prior_labels=envelope.prior_labels,
        snippet=envelope.snippet,
        normalized_body=triage_body,
        extracted_links=envelope.extracted_links,
        attachment_metadata=envelope.attachment_metadata,
        recommended_action=action,
        draft_reply=draft,
        draft_created=draft is not None,
        style_profile_used=style_profile_used and draft is not None,
        style_profile_id=(
            style_profile.profile_id if style_profile_used and draft is not None else ""
        ),
        approval_required=draft is not None,
        requires_human_review=bool(risk_flags or draft),
    )


def triage_gmail_message_envelope(
    envelope: GmailMessageEnvelope,
    *,
    email_style_profile: EmailStyleProfile | dict[str, Any] | None = None,
) -> EmailTriageResult:
    """Triage a normalized Gmail envelope while preserving sanitized context fields."""

    body_with_prescreen = "\n".join(
        [
            envelope.normalized_body,
            *[link.url for link in envelope.extracted_links],
            *envelope.suspicious_signals,
        ]
    ).strip()
    result = triage_email_fixture(
        EmailFixture(
            subject=envelope.subject,
            body=body_with_prescreen,
            sender_name=envelope.sender_name,
            sender_email=envelope.sender_email,
            message_id=envelope.message_id or "gmail-message",
            thread_id=envelope.thread_id,
            received_at=envelope.received_at,
            snippet=envelope.snippet,
            prior_labels=tuple(envelope.prior_labels),
        ),
        email_style_profile=email_style_profile,
    )
    suspicious_signals = list(
        dict.fromkeys([*result.suspicious_signals, *envelope.suspicious_signals])
    )
    limitations = list(dict.fromkeys([*result.triage_limitations, *envelope.triage_limitations]))
    if envelope.attachment_metadata:
        limitations.append("Attachments were screened by metadata only and not ingested.")
    return result.model_copy(
        update={
            "thread_id": envelope.thread_id,
            "received_at": envelope.received_at,
            "prior_labels": envelope.prior_labels,
            "snippet": envelope.snippet,
            "normalized_body": envelope.normalized_body,
            "extracted_links": envelope.extracted_links,
            "attachment_metadata": envelope.attachment_metadata,
            "thread_summary": envelope.thread_summary,
            "thread_context": envelope.thread_context,
            "suspicious_signals": suspicious_signals,
            "triage_limitations": list(dict.fromkeys(limitations)),
            "requires_human_review": bool(
                result.risk_flags or result.draft_reply or suspicious_signals
            ),
        }
    )


def group_gmail_envelopes_fixture(
    envelopes: list[GmailMessageEnvelope],
    *,
    operator_request: str,
    lookback_days: int,
    source_label: str = "INBOX",
) -> GmailPriorityGroupingResult:
    """Prioritize a bounded sanitized Gmail batch without a model or provider write."""

    buckets: dict[str, list[GmailPriorityGroupedMessage]] = {
        "urgent": [],
        "important": [],
        "can_wait": [],
        "ignore": [],
    }
    for envelope in envelopes:
        triage = triage_gmail_message_envelope(envelope)
        if triage.priority == "urgent" or any(
            flag in {"security", "possible_phi"} for flag in triage.risk_flags
        ):
            bucket = "urgent"
        elif triage.needs_reply or triage.priority == "high":
            bucket = "important"
        elif triage.category in {"newsletter", "vendor"}:
            bucket = "ignore"
        else:
            bucket = "can_wait"

        buckets[bucket].append(
            GmailPriorityGroupedMessage(
                message_id=triage.message_id,
                thread_id=triage.thread_id,
                received_at=triage.received_at,
                subject=triage.subject,
                sender_name=triage.sender_name,
                sender_email=triage.sender_email,
                bucket=bucket,
                category=triage.category,
                confidence=triage.confidence,
                priority=triage.priority,
                summary=triage.summary,
                reasoning=triage.reasoning,
                needs_reply=triage.needs_reply,
                recommended_action=triage.recommended_action,
                recommended_labels=triage.recommended_labels,
                risk_flags=triage.risk_flags,
                draft_reply=None,
                draft_created=False,
                approval_required=False,
                requires_human_review=triage.requires_human_review,
                send_enabled=False,
                sent=False,
            )
        )

    for messages in buckets.values():
        messages.sort(key=lambda message: message.received_at, reverse=True)
    return GmailPriorityGroupingResult(
        request_summary=operator_request,
        source_label=source_label,
        lookback_days=lookback_days,
        source_message_count=len(envelopes),
        urgent=buckets["urgent"],
        important=buckets["important"],
        can_wait=buckets["can_wait"],
        ignore=buckets["ignore"],
        draft_count=0,
        send_enabled=False,
        sent=False,
        live_side_effects_enabled=False,
        audit_notes=[
            "Deterministic grouping used only the supplied sanitized bounded message set.",
            "No provider draft, label change, or send action was attempted.",
        ],
    )


def run_gmail_triage_fixture(
    fixture: str | Path | None = None,
    *,
    subject: str = "",
    sender_name: str = "",
    sender_email: str = "",
    email_style_profile: EmailStyleProfile | dict[str, Any] | None = None,
) -> EmailTriageResult:
    """Load and triage a fixture email without invoking the SDK runner."""

    return triage_email_fixture(
        load_email_fixture(
            fixture,
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
        ),
        email_style_profile=email_style_profile,
    )


def run_gmail_triage_sdk(
    typed_input: GmailTriageSDKInput | GmailMessageEnvelope | str,
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    context_flags: Mapping[str, bool] | None = None,
    tool_tier: str | int | None = None,
    max_turns: int | None = None,
    attach_tools: bool = True,
    compact_instructions: bool = False,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    provider_selection_required: bool | None = None,
    provider_context_read_required: bool | None = None,
    repair_invalid_selection: bool = True,
    prepared_agent: Agent | None = None,
    selected_context_callback: Callable[[GmailReadContextResult], None] | None = None,
) -> TypedAgentRunResult[EmailTriageResult]:
    """Run Gmail triage, optionally retaining a host-admitted nested toolbox."""

    if isinstance(typed_input, str):
        typed_input = GmailTriageSDKInput(
            subject="",
            body="",
            request=typed_input,
        )
    elif isinstance(typed_input, GmailMessageEnvelope):
        typed_input = GmailTriageSDKInput.from_envelope(typed_input)
    if not typed_input.request_evaluated_at.strip():
        typed_input = replace(
            typed_input,
            request_evaluated_at=(
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            ),
        )
    if typed_input.model_request_capacity is None:
        model_request_capacity = current_model_request_capacity()
        if model_request_capacity is not None:
            typed_input = replace(
                typed_input,
                model_request_capacity=model_request_capacity,
            )
    selection_required = (
        _gmail_provider_selection_required(typed_input)
        if provider_selection_required is None
        else bool(provider_selection_required)
    )
    context_read_required = (
        _gmail_verified_context_read_required(typed_input)
        if provider_context_read_required is None
        else bool(provider_context_read_required)
    )
    resolved_tool_tier = tool_tier or _default_gmail_triage_sdk_tool_tier(manual_request_plan)
    # Select procedures from the task, not dataclass field names, style examples,
    # or the host's advisory memo. Those remain visible in to_prompt().
    task_text = typed_input.request.strip() or "\n".join(
        value for value in (typed_input.subject, typed_input.body) if value
    )
    agent = prepared_agent or build_gmail_triage_agent(
        model=model,
        include_tools=attach_tools,
        provider_tools_live=live,
        provider_selection_mode=selection_required,
        request_text=task_text,
        context_flags=context_flags,
        tool_tier=resolved_tool_tier,
        compact_instructions=compact_instructions,
        manual_request_plan=manual_request_plan,
        tool_scope_mode=ToolScopeMode.REQUEST_SCOPED,
    )
    turn_policy = resolve_sdk_turn_policy(
        "gmail_triage",
        request_text=task_text,
        explicit_max_turns=max_turns,
    )
    scope_receipt = tool_scope_receipt_for_agent(agent)
    tool_execution_contract = (
        ToolExecutionContract.required(
            ToolEvidenceGroup(
                name="gmail_query",
                any_of_tool_names=("query_gmail_message_summaries",),
            ),
            stage="gmail_agent_owned_selection",
        )
        if selection_required
        else ToolExecutionContract.required(
            ToolEvidenceGroup(
                name="gmail_verified_context_read",
                any_of_tool_names=("read_gmail_context",),
            ),
            stage="gmail_verified_continuation_read",
        )
        if context_read_required
        else None
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=EmailTriageResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=max(turn_policy.max_turns, 7) if selection_required else turn_policy.max_turns,
        trace_metadata=tool_scope_trace_metadata_for_agent(agent),
        tool_execution_contract=tool_execution_contract,
        structured_retry_evidence_provider=(
            lambda: gmail_model_read_evidence_snapshot(agent.tools)
            if selection_required
            else ()
        ),
    )
    if selection_required:
        result = _validate_and_repair_gmail_selection(
            result,
            typed_input=typed_input,
            run_config=run_config,
            live=live,
            model=model,
            session=session,
            compact_instructions=compact_instructions,
            repair_invalid_selection=repair_invalid_selection,
            cumulative_tool_evidence=gmail_model_read_evidence_snapshot(agent.tools),
        )
    elif context_read_required:
        result = _validate_and_repair_verified_gmail_continuation(
            result,
            typed_input=typed_input,
            run_config=run_config,
            live=live,
            model=model,
            session=session,
            compact_instructions=compact_instructions,
            repair_invalid_selection=repair_invalid_selection,
        )
    request_cache = getattr(result, "request_cache", None)
    if isinstance(request_cache, dict):
        request_cache["request_tool_scope"] = scope_receipt
    if selected_context_callback is not None and (selection_required or context_read_required):
        context = selected_provider_context(
            result.output, gmail_model_read_evidence_snapshot(agent.tools),
        )
        if context is not None:
            selected_context_callback(context)
    return result


class GmailAgentDecisionError(RuntimeError):
    """The Gmail model did not produce a valid choice after bounded repair."""

    def __init__(
        self,
        telemetry: Mapping[str, Any],
        *,
        result: TypedAgentRunResult[EmailTriageResult] | None = None,
    ) -> None:
        self.telemetry = dict(telemetry)
        self.result = result
        outcome = self.telemetry.get("validator_outcome")
        reason = outcome.get("reason_code") if isinstance(outcome, Mapping) else ""
        super().__init__(
            "Gmail Triage could not bind its selected message/thread to the bounded "
            f"provider evidence after one repair attempt. reason_code={reason or 'unknown'}"
        )


def _build_gmail_selection_repair_agent(*, model: str | None) -> Agent:
    """Build an isolated, tool-free repair stage with a mutually exclusive schema."""

    return build_sdk_agent(
        name="gmail_triage_selection_repair",
        instructions=compose_direct_instructions(
            "safety_policy.md",
            "gmail_triage.md",
        ),
        output_type=GmailSelectionRepairResult,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="gmail_triage",
        handoff_description=(
            "Use only for one bounded validator repair over already-read Gmail evidence."
        ),
    )


def _unresolved_gmail_repair_result(
    initial: EmailTriageResult,
    repair: GmailNeedsMoreContextRepair,
) -> EmailTriageResult:
    """Render the agent's explicit abstention without selecting or retaining evidence."""

    requested_context = list(repair.requested_context)
    action = (
        "Provide the missing context requested by Gmail Triage: " + "; ".join(requested_context)
        if requested_context
        else "Provide additional context so Gmail Triage can select without guessing."
    )
    return initial.model_copy(
        update={
            "message_id": "",
            "thread_id": "",
            "received_at": "",
            "subject": "",
            "sender_name": "",
            "sender_email": "",
            "category": "unrelated",
            "confidence": 0.0,
            "priority": "normal",
            "summary": "Gmail Triage needs more context before selecting a conversation.",
            "operator_answer": "",
            "thread_summary": "",
            "thread_context": "",
            "reasoning": repair.reasoning,
            "needs_reply": False,
            "recommended_labels": [],
            "risk_flags": [],
            "suspicious_signals": [],
            "recommended_next_agent": "human_review",
            "triage_limitations": list(repair.limitations),
            "prior_labels": [],
            "snippet": "",
            "normalized_body": "",
            "retrieval_diagnostics": {},
            "extracted_links": [],
            "attachment_metadata": [],
            "recommended_action": action,
            "draft_reply": None,
            "draft_created": False,
            "style_profile_used": False,
            "style_profile_id": "",
            "approval_required": False,
            "requires_human_review": True,
            "decision": AgentDecisionRecord(
                decision_owner="specialist_agent",
                decision_stage="gmail_candidate_selection",
                reasoning=repair.reasoning,
                limitations=list(repair.limitations),
                needs_more_context=True,
            ),
        }
    )


def _gmail_provider_selection_required(typed_input: GmailTriageSDKInput) -> bool:
    """Require the model tool loop only when no exact provider object was supplied."""

    return bool(
        typed_input.request.strip()
        and not (typed_input.message_id.strip() or typed_input.thread_id.strip())
        and not (
            typed_input.subject.strip()
            or typed_input.body.strip()
            or typed_input.snippet.strip()
            or typed_input.thread_context.strip()
        )
    )


def _gmail_verified_context_read_required(typed_input: GmailTriageSDKInput) -> bool:
    return bool(
        typed_input.request.strip()
        and (typed_input.message_id.strip() or typed_input.thread_id.strip())
        and not (
            typed_input.subject.strip()
            or typed_input.body.strip()
            or typed_input.snippet.strip()
            or typed_input.thread_context.strip()
        )
    )


def _validate_and_repair_gmail_selection(
    result: TypedAgentRunResult[EmailTriageResult],
    *,
    typed_input: GmailTriageSDKInput,
    run_config: Any | None,
    live: bool,
    model: str | None,
    session: Any | None,
    compact_instructions: bool,
    repair_invalid_selection: bool,
    cumulative_tool_evidence: tuple[dict[str, Any], ...] = (),
) -> TypedAgentRunResult[EmailTriageResult]:
    evidence = gmail_decision_evidence(
        result.raw_result,
        cumulative_tool_evidence=cumulative_tool_evidence,
    )
    normalized_output = result.final_output.model_copy(
        update={"decision": decision_record_from_gmail_result(result.final_output)}
    )
    validator = validate_gmail_agent_decision(
        normalized_output,
        evidence,
        original_request=typed_input.request,
        require_live_provider=live,
    )
    normalization_metadata = []
    if validator.reason_code == "needs_more_context_with_claimed_gmail_output":
        normalized_output, cleared_fields = normalize_empty_gmail_abstention(
            normalized_output, evidence,
        )
        if cleared_fields:
            normalization_metadata = [{
                "kind": "verified_empty_search_context_discarded", "fields": list(cleared_fields),
            }]
            validator = validate_gmail_agent_decision(
                normalized_output, evidence, original_request=typed_input.request,
                require_live_provider=live,
            )
    normalized_result = replace(result, output=normalized_output)
    if validator.status == "accepted":
        telemetry = gmail_decision_telemetry(normalized_output, validator, evidence)
        if normalization_metadata:
            telemetry["repair_metadata_normalizations"] = normalization_metadata
        return _attach_gmail_decision_telemetry(
            normalized_result, telemetry,
        )
    if validator.reason_code in {
        "gmail_live_query_evidence_missing",
        "gmail_live_context_evidence_missing",
    }:
        telemetry = gmail_decision_telemetry(normalized_output, validator, evidence)
        raise GmailAgentDecisionError(telemetry, result=normalized_result)
    if not repair_invalid_selection:
        telemetry = gmail_decision_telemetry(normalized_output, validator, evidence)
        raise GmailAgentDecisionError(telemetry, result=normalized_result)

    repair_request = (
        typed_input.request + "\n\nDeterministic selection validation rejected the first decision. "
        "Correct only the candidate selection and final triage using the verified, "
        "already-read evidence below. Do not invent an identity and do not call tools. "
        "Return exactly one of these mutually exclusive shapes:\n"
        "RESOLVED: decision.needs_more_context=false; select exactly one identity "
        "from decision_candidate_ids; return only matching message/thread fields; "
        "assess every successfully read decision candidate; and produce reply output "
        "only when supported by that selected context. Explicitly return "
        "decision.decision_owner=specialist_agent and "
        "decision.decision_stage=gmail_candidate_selection; do not rely on defaults.\n"
        "When the rejected result selected a verified thread but paired it with a "
        "message_id from another returned record, preserve the agent's semantic thread "
        "choice, set thread_id and decision.selected_candidate_id to that exact returned "
        "thread_id, and leave message_id blank. A unique returned message-to-thread "
        "mapping is verified evidence, not missing context. You still choose whether the "
        "selected conversation is semantically supported.\n"
        "UNRESOLVED: decision.needs_more_context=true; clear message_id, thread_id, "
        "received_at, subject, sender fields, thread fields, snippet, normalized_body, "
        "draft_reply, selected_candidate_id, selected_candidate_ids, candidate_assessments, "
        "labels, risk flags, links, attachments, and every provider-specific claim. "
        "Do not preserve desired reply copy merely because the original request asked for it.\n"
        + json.dumps(
            {
                "validator_feedback": validator.model_dump(mode="json"),
                "conflicting_output_fields": list(gmail_claimed_output_fields(normalized_output)),
                "permitted_resolutions": ["selected", "needs_more_context"],
                "verified_candidate_evidence": evidence.repair_context(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    repair_input = replace(
        typed_input,
        request=repair_request,
        subject="",
        body="",
        sender_name="",
        sender_email="",
        message_id="",
        thread_id="",
        received_at="",
        snippet="",
        prior_labels=[],
        extracted_links=[],
        attachment_metadata=[],
        thread_summary="",
        thread_context="",
        suspicious_signals=[],
        triage_limitations=[
            "Validator repair used the already-read bounded Gmail candidate evidence; "
            "no provider query was repeated."
        ],
    )
    repair_agent = _build_gmail_selection_repair_agent(model=model)
    repair_result = run_typed_sdk_agent(
        agent=repair_agent,
        typed_input=repair_input,
        output_type=GmailSelectionRepairResult,
        run_config=run_config,
        live=live,
        session=None,
        inherit_env_session=False,
        max_turns=2,
        tool_execution_contract=ToolExecutionContract(
            mode=ToolExecutionMode.FORBIDDEN,
            stage="gmail_validator_repair",
        ),
    )
    repair_branch = repair_result.final_output.repair
    repair_metadata_normalizations: list[dict[str, Any]] = []
    if isinstance(repair_branch, GmailSelectedSelectionRepair):
        repair_decision = repair_branch.triage_result.decision
        if "decision" in repair_branch.triage_result.model_fields_set:
            # The repair agent owns the selected identity, assessments, reasoning,
            # limitations, and reply judgment. The stage name is fixed runtime
            # provenance, not a semantic choice; stamp it at this boundary so an
            # omitted string literal cannot discard an otherwise valid repair.
            if repair_decision.decision_stage != "gmail_candidate_selection":
                repair_metadata_normalizations.append(
                    {
                        "field": "decision_stage",
                        "reported_value": repair_decision.decision_stage,
                        "canonical_value": "gmail_candidate_selection",
                        "semantic_selection_changed": False,
                    }
                )
            repair_decision = repair_decision.model_copy(
                update={"decision_stage": "gmail_candidate_selection"}
            )
        repaired_output = repair_branch.triage_result.model_copy(
            update={"decision": repair_decision}
        )
    else:
        repaired_output = _unresolved_gmail_repair_result(
            normalized_output,
            repair_branch,
        )
    repaired_validator = validate_gmail_agent_decision(
        repaired_output,
        evidence,
        original_request=typed_input.request,
        repair_attempted=True,
        require_live_provider=live,
    )
    telemetry = gmail_decision_telemetry(repaired_output, repaired_validator, evidence)
    telemetry["repair_metadata_normalizations"] = repair_metadata_normalizations
    telemetry["model_stages"] = [
        {
            "stage": "gmail_agent_owned_selection",
            "tool_mode": "model_called",
            "validator_status": validator.status,
            "reason_code": validator.reason_code,
        },
        {
            "stage": "gmail_validator_repair",
            "tool_mode": "verified_context_tool_free",
            "validator_status": repaired_validator.status,
            "reason_code": repaired_validator.reason_code,
        },
    ]
    terminal_events = list(telemetry.get("events") or [])
    telemetry["events"] = [
        {
            "event_type": "proposed",
            "decision_owner": normalized_output.decision.decision_owner,
            "decision_stage": "gmail_candidate_selection",
            "attempt": 1,
            "candidate_ids": list(evidence.decision_candidate_ids),
            "selected_candidate_ids": list(normalized_output.decision.selected_candidate_ids),
            "excluded_candidate_ids": [
                item.candidate_id
                for item in normalized_output.decision.candidate_assessments
                if item.disposition == "excluded"
            ],
            "validator_status": "not_evaluated",
            "reason_code": "",
            "tool_mode": "model_called",
        },
        {
            "event_type": "validator_result",
            "decision_owner": normalized_output.decision.decision_owner,
            "decision_stage": "gmail_candidate_selection",
            "attempt": 1,
            "candidate_ids": list(evidence.decision_candidate_ids),
            "selected_candidate_ids": list(normalized_output.decision.selected_candidate_ids),
            "excluded_candidate_ids": [
                item.candidate_id
                for item in normalized_output.decision.candidate_assessments
                if item.disposition == "excluded"
            ],
            "validator_status": validator.status,
            "reason_code": validator.reason_code,
            "tool_mode": "model_called",
        },
        {
            "event_type": "repair_proposed",
            "decision_owner": repaired_output.decision.decision_owner,
            "decision_stage": "gmail_candidate_selection",
            "attempt": 2,
            "candidate_ids": list(evidence.decision_candidate_ids),
            "selected_candidate_ids": list(repaired_output.decision.selected_candidate_ids),
            "excluded_candidate_ids": [
                item.candidate_id
                for item in repaired_output.decision.candidate_assessments
                if item.disposition == "excluded"
            ],
            "validator_status": "not_evaluated",
            "reason_code": validator.reason_code,
            "tool_mode": "verified_context_tool_free",
        },
        *[
            event
            for event in terminal_events
            if event.get("event_type") in {"validator_result", "terminal"}
        ],
    ]
    combined_result = _combine_gmail_model_results(
        normalized_result,
        replace(repair_result, output=repaired_output),
        telemetry=telemetry,
    )
    if repaired_validator.status != "accepted":
        raise GmailAgentDecisionError(telemetry, result=combined_result)
    return combined_result


def _validate_and_repair_verified_gmail_continuation(
    result: TypedAgentRunResult[EmailTriageResult],
    *,
    typed_input: GmailTriageSDKInput,
    run_config: Any | None,
    live: bool,
    model: str | None,
    session: Any | None,
    compact_instructions: bool,
    repair_invalid_selection: bool,
) -> TypedAgentRunResult[EmailTriageResult]:
    """Repair one invalid continuation choice without another Gmail read."""

    output = result.final_output.model_copy(
        update={"decision": decision_record_from_gmail_result(result.final_output)}
    )
    normalized_result = replace(result, output=output)
    validator = validate_verified_gmail_continuation_decision(
        output,
        result.raw_result,
        expected_message_id=typed_input.message_id,
        expected_thread_id=typed_input.thread_id,
    )
    evidence = gmail_decision_evidence(result.raw_result)
    telemetry = gmail_decision_telemetry(output, validator, evidence)
    telemetry["context_source"] = "verified_continuation_object"
    if validator.status == "accepted":
        return _attach_gmail_decision_telemetry(normalized_result, telemetry)
    if not repair_invalid_selection:
        raise GmailAgentDecisionError(telemetry, result=normalized_result)

    repair_request = (
        typed_input.request
        + "\n\nDeterministic continuation validation rejected the first semantic "
        "choice. Correct only the reply/message/thread identity against the exact "
        "verified continuation context below. Do not query Gmail, do not read Gmail "
        "again, and do not reinterpret phrases such as 'same thread'.\n"
        + json.dumps(
            {
                "validator_feedback": validator.model_dump(mode="json"),
                "expected_message_id": typed_input.message_id,
                "expected_thread_id": typed_input.thread_id,
                "verified_continuation_context": evidence.repair_context(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    repair_input = replace(typed_input, request=repair_request)
    repair_agent = build_gmail_triage_agent(
        model=model,
        include_tools=False,
        request_text=typed_input.request,
        compact_instructions=compact_instructions,
        tool_scope_mode=ToolScopeMode.REQUEST_SCOPED,
    )
    repair_result = run_typed_sdk_agent(
        agent=repair_agent,
        typed_input=repair_input,
        output_type=EmailTriageResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=2,
        tool_execution_contract=ToolExecutionContract(
            mode=ToolExecutionMode.FORBIDDEN,
            stage="gmail_verified_continuation_repair",
        ),
    )
    repaired_output = repair_result.final_output.model_copy(
        update={"decision": decision_record_from_gmail_result(repair_result.final_output)}
    )
    repaired_validator = validate_verified_gmail_continuation_decision(
        repaired_output,
        result.raw_result,
        expected_message_id=typed_input.message_id,
        expected_thread_id=typed_input.thread_id,
        repair_attempted=True,
    )
    repaired_telemetry = gmail_decision_telemetry(
        repaired_output,
        repaired_validator,
        evidence,
    )
    repaired_telemetry.update(
        {
            "context_source": "verified_continuation_object",
            "model_stages": [
                {
                    "stage": "gmail_verified_continuation",
                    "tool_mode": "model_called",
                    "validator_status": validator.status,
                    "reason_code": validator.reason_code,
                },
                {
                    "stage": "gmail_verified_continuation_repair",
                    "tool_mode": "verified_context_tool_free",
                    "validator_status": repaired_validator.status,
                    "reason_code": repaired_validator.reason_code,
                },
            ],
        }
    )
    combined_result = _combine_gmail_model_results(
        normalized_result,
        replace(repair_result, output=repaired_output),
        telemetry=repaired_telemetry,
    )
    if repaired_validator.status != "accepted":
        raise GmailAgentDecisionError(repaired_telemetry, result=combined_result)
    return combined_result


def _attach_gmail_decision_telemetry(
    result: TypedAgentRunResult[EmailTriageResult],
    telemetry: Mapping[str, Any],
) -> TypedAgentRunResult[EmailTriageResult]:
    request_cache = dict(result.request_cache or {})
    request_cache["decision_ownership"] = dict(telemetry)
    repeated_queries = int(telemetry.get("repeated_query_call_count") or 0)
    if repeated_queries:
        request_cache["tool_corrections"] = max(
            int(request_cache.get("tool_corrections") or 0),
            min(repeated_queries, 1),
        )
    return replace(result, request_cache=request_cache)


def _combine_gmail_model_results(
    initial: TypedAgentRunResult[EmailTriageResult],
    repaired: TypedAgentRunResult[EmailTriageResult],
    *,
    telemetry: Mapping[str, Any],
) -> TypedAgentRunResult[EmailTriageResult]:
    initial_items = list(getattr(initial.raw_result, "new_items", []) or [])
    repair_items = list(getattr(repaired.raw_result, "new_items", []) or [])
    request_cache = dict(initial.request_cache or {})
    request_cache["decision_ownership"] = dict(telemetry)
    request_cache["repair_stage_request_cache"] = dict(repaired.request_cache or {})
    request_cache["decision_repairs"] = int(
        request_cache.get("decision_repairs") or 0
    ) + 1
    return TypedAgentRunResult(
        agent_name=repaired.agent_name,
        output=repaired.final_output,
        raw_result=SimpleNamespace(new_items=[*initial_items, *repair_items]),
        live=repaired.live,
        usage=_sum_numeric_mappings(initial.usage, repaired.usage),
        cost=_sum_numeric_mappings(initial.cost, repaired.cost),
        budget_guard=dict(repaired.budget_guard or initial.budget_guard or {}),
        request_cache=request_cache,
        execution_telemetry={
            "schema": "keystone.gmail.multi_stage_execution.v1",
            "initial": dict(initial.execution_telemetry or {}),
            "repair": dict(repaired.execution_telemetry or {}),
        },
        tool_receipts=[*initial.tool_receipts, *repaired.tool_receipts],
    )


def _sum_numeric_mappings(
    first: Mapping[str, Any] | None,
    second: Mapping[str, Any] | None,
) -> dict[str, Any]:
    output: dict[str, Any] = dict(first or {})
    for key, value in (second or {}).items():
        prior = output.get(key)
        if key == "available":
            output[key] = prior is True and value is True
        elif isinstance(prior, bool) and isinstance(value, bool):
            output[key] = prior or value
        elif (
            isinstance(prior, int | float)
            and not isinstance(prior, bool)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            output[key] = prior + value
        elif key not in output:
            output[key] = value
    return output


def run_gmail_priority_grouping_sdk(
    typed_input: GmailPriorityGroupingSDKInput | list[GmailMessageEnvelope],
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int | None = None,
) -> TypedAgentRunResult[GmailPriorityGroupingResult]:
    """Run batch Gmail priority grouping through the typed SDK harness."""

    if isinstance(typed_input, list):
        typed_input = GmailPriorityGroupingSDKInput.from_envelopes(typed_input)
    agent = build_gmail_priority_grouping_agent(
        model=model,
        request_text=skill_request_text(typed_input),
    )
    turn_policy = resolve_sdk_turn_policy(
        "gmail_priority_grouping",
        request_text=skill_request_text(typed_input),
        explicit_max_turns=max_turns,
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=GmailPriorityGroupingResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=turn_policy.max_turns,
    )
    if not isinstance(result, TypedAgentRunResult):
        return result
    authoritative_summary = typed_input.operator_request or typed_input.request
    normalized_output = result.final_output.model_copy(
        update={
            "request_summary": authoritative_summary,
            "source_label": typed_input.source_label,
            "lookback_days": typed_input.lookback_days,
            "source_message_count": len(typed_input.messages),
        }
    )
    return replace(result, output=normalized_output)


def run_gmail_candidate_ranking_sdk(
    typed_input: GmailCandidateRankingSDKInput,
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int | None = None,
) -> TypedAgentRunResult[GmailCandidateRankingResult]:
    """Run selection-only Gmail candidate reasoning through the SDK."""

    agent = build_gmail_candidate_ranking_agent(
        model=model,
        request_text=typed_input.operator_request,
    )
    turn_policy = resolve_sdk_turn_policy(
        "gmail_priority_grouping",
        request_text=typed_input.operator_request,
        explicit_max_turns=max_turns,
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=GmailCandidateRankingResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=turn_policy.max_turns,
    )
    if not isinstance(result, TypedAgentRunResult):
        return result
    normalized_output = result.final_output.model_copy(
        update={
            "request_summary": typed_input.operator_request,
            "source_message_count": len(typed_input.messages),
        }
    )
    return replace(result, output=normalized_output)


def run_gmail_contact_lookup_sdk(
    typed_input: GmailContactLookupSDKInput,
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int | None = None,
) -> TypedAgentRunResult[GmailContactLookupResult]:
    """Run one bounded Gmail contact-evidence answer through the SDK."""

    agent = build_gmail_contact_lookup_agent(
        model=model,
        request_text=typed_input.operator_request,
    )
    turn_policy = resolve_sdk_turn_policy(
        "gmail_triage",
        request_text=typed_input.operator_request,
        explicit_max_turns=max_turns,
    )
    return run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=GmailContactLookupResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=turn_policy.max_turns,
    )


def build_gmail_triage_agent(
    model: str | None = None,
    *,
    include_tools: bool = True,
    provider_tools_live: bool = False,
    provider_selection_mode: bool = False,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    compact_instructions: bool = False,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    tool_scope_mode: ToolScopeMode | str = ToolScopeMode.AUTO,
) -> Agent:
    """Build the Gmail triage agent."""

    skill_files = select_agent_skill_names(
        "gmail_triage",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    composer = compose_direct_instructions if compact_instructions else compose_instructions
    prompt_files = (
        ("keystone_profile.md", "safety_policy.md", "gmail_triage.md")
        if compact_instructions
        else ("keystone_profile.md", "safety_policy.md", "tools.md", "gmail_triage.md")
    )
    instructions = composer(*prompt_files, skill_files=skill_files)
    (
        inspect_gmail_mailbox_schema,
        query_gmail_message_summaries,
        read_gmail_context,
    ) = gmail_model_read_tools(live=provider_tools_live)
    tools = (
        [
            inspect_gmail_mailbox_schema,
            query_gmail_message_summaries,
            read_gmail_context,
            get_gmail_message,
            apply_gmail_labels,
            modify_gmail_message_state,
            create_gmail_draft_with_attachment,
            create_gmail_draft_reply,
            gmail_test_draft_lifecycle,
            send_gmail_test_draft,
            load_email_style_profile,
            list_local_context_sources,
            search_local_context,
            read_local_context_file,
            retrieve_memory,
            airtable_get_base_schema,
            airtable_read_records,
            airtable_write_record,
            search_web,
            structure_web_data_for_schema,
            list_outreach_tracking_records,
            create_approval_queue_item,
            *google_workspace_tools(),
        ]
        if include_tools
        else []
    )
    if include_tools and _marked_test_draft_lifecycle_request(request_text):
        tools = [gmail_test_draft_lifecycle]
    elif include_tools and is_gmail_schema_only_request(request_text):
        tools = [inspect_gmail_mailbox_schema]
    elif include_tools and provider_selection_mode:
        tools = [query_gmail_message_summaries, read_gmail_context]
    resolved_scope_mode = tool_scope_mode
    if str(tool_scope_mode) == ToolScopeMode.AUTO.value and (
        request_text or manual_request_plan is not None or tool_tier is not None
    ):
        resolved_scope_mode = ToolScopeMode.REQUEST_SCOPED
    if _marked_test_draft_lifecycle_request(request_text):
        required_tool_names = ("gmail_test_draft_lifecycle",)
    else:
        required_tool_names = ()
    attachment = scope_tools_for_request(
        "gmail_triage",
        tools,
        manual_request_plan=manual_request_plan,
        tool_tier=tool_tier,
        mode=resolved_scope_mode,
        required_tool_names=required_tool_names,
    )
    agent = build_sdk_agent(
        name="gmail_triage",
        instructions=instructions,
        output_type=EmailTriageResult,
        tools=list(attachment.tools),
        guardrails=keystone_guardrails(),
        model=model,
        model_settings=build_model_settings(
            reasoning_effort=(
                gmail_selection_reasoning_effort(model) if provider_selection_mode else None
            ),
            tool_choice=("query_gmail_message_summaries" if provider_selection_mode else None)
        ),
        policy_agent_name="gmail_triage",
        handoff_description=(
            "Use for inbound email classification, label planning, suspicious message review, "
            "and draft-only reply preparation."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)


def _marked_test_draft_lifecycle_request(request_text: str) -> bool:
    """Recognize one bounded marker-gated draft lifecycle already owned by one tool."""

    normalized = " ".join(str(request_text or "").lower().split())
    return bool(
        re.search(r"\bkba_test_draft(?:_[a-z0-9]+)*\b", normalized)
        and re.search(r"\b(?:add|create|make|write)\b", normalized)
        and re.search(r"\b(?:update|change|modify|revise|edit)\b", normalized)
        and re.search(r"\b(?:delete|remove|clean\s*up)\b", normalized)
    )


def is_gmail_schema_only_request(request_text: str) -> bool:
    """Recognize a metadata-only connector question that forbids mailbox reads."""

    normalized = " ".join(
        str(request_text or "")
        .lower()
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .split()
    )
    asks_for_schema = bool(
        re.search(
            r"\b(?:schema|available\s+(?:gmail\s+)?fields?|field\s+names?|"
            r"field\s+types?)\b",
            normalized,
        )
    )
    forbids_message_data = bool(
        re.search(
            r"\b(?:do\s+not|don't|dont|without)\b[^.;]{0,100}"
            r"\b(?:query|search|read|return|include|expose)\b[^.;]{0,80}"
            r"\b(?:inbox|mail|messages?|mailbox(?:-derived)?\s+data|senders?|subjects?|"
            r"snippets?|body|content)\b",
            normalized,
        )
    )
    return asks_for_schema and forbids_message_data


def build_gmail_mailbox_action_agent(model: str | None = None) -> Agent:
    """Build a one-turn no-tool interpreter for an exact Gmail state request."""

    return build_sdk_agent(
        name="gmail_triage",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "gmail_mailbox_action.md",
        ),
        output_type=GmailMailboxActionPlan,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        handoff_description=(
            "Interpret one exact-message mailbox-state request; Python executes and verifies it."
        ),
    )


def _default_gmail_triage_sdk_tool_tier(
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> str:
    """Resolve Gmail's tier only from canonical semantic authority."""

    return default_tool_tier_for_request(manual_request_plan)


def build_gmail_priority_grouping_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    include_all_skills: bool = False,
) -> Agent:
    """Build the LLM-only Gmail priority grouping agent."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "gmail_triage.md",
        "gmail_priority_grouping.md",
        skill_files=select_agent_skill_names(
            "gmail_triage",
            request_text=request_text,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="gmail_triage",
        instructions=instructions,
        output_type=GmailPriorityGroupingResult,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        handoff_description=(
            "Use for batch Gmail priority grouping with urgent-only draft guidance and "
            "no live side effects."
        ),
    )


def build_gmail_candidate_ranking_agent(
    model: str | None = None,
    *,
    request_text: str = "",
) -> Agent:
    """Build the selection-only Gmail candidate-ranking agent."""

    return build_sdk_agent(
        name="gmail_triage",
        instructions=compose_direct_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "gmail_candidate_ranking.md",
            skill_files=select_agent_skill_names(
                "gmail_triage",
                request_text=request_text,
                compact=True,
            ),
        ),
        output_type=GmailCandidateRankingResult,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="gmail_triage",
        handoff_description=(
            "Use for read-only semantic selection from one bounded Gmail provider result."
        ),
    )


def build_gmail_contact_lookup_agent(
    model: str | None = None,
    *,
    request_text: str = "",
) -> Agent:
    """Build the no-tool specialist for bounded Gmail contact evidence."""

    return build_sdk_agent(
        name="gmail_triage",
        instructions=compose_direct_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "gmail_contact_lookup.md",
            skill_files=select_agent_skill_names(
                "gmail_triage",
                request_text=request_text,
                compact=True,
            ),
        ),
        output_type=GmailContactLookupResult,
        tools=[],
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="gmail_triage",
        handoff_description=(
            "Use for read-only known-contact resolution from bounded Gmail evidence."
        ),
    )
