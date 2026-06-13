"""Gmail triage agent builder and deterministic fixture-mode triage."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
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
from keystone_agents.models import (
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.email_triage import (
    EmailTriageResult,
    GmailMessageEnvelope,
    GmailPriorityGroupingResult,
    managed_gmail_labels,
)
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text
from keystone_agents.tools.approval_tool import create_approval_queue_item
from keystone_agents.tools.email_style_tool import load_email_style_profile
from keystone_agents.tools.gmail_tool import (
    apply_gmail_labels,
    create_gmail_draft_reply,
    get_gmail_message,
    gmail_message_envelope_from_dict,
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
) -> TypedAgentRunResult[EmailTriageResult]:
    """Run Gmail triage through the typed SDK harness."""

    if isinstance(typed_input, GmailMessageEnvelope):
        typed_input = GmailTriageSDKInput.from_envelope(typed_input)
    resolved_tool_tier = tool_tier or _default_gmail_triage_sdk_tool_tier(typed_input)
    agent = build_gmail_triage_agent(
        model=model,
        request_text=skill_request_text(typed_input),
        context_flags=context_flags,
        tool_tier=resolved_tool_tier,
    )
    return run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=EmailTriageResult,
        run_config=run_config,
        live=live,
        session=session,
    )


def run_gmail_priority_grouping_sdk(
    typed_input: GmailPriorityGroupingSDKInput | list[GmailMessageEnvelope],
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
) -> TypedAgentRunResult[GmailPriorityGroupingResult]:
    """Run batch Gmail priority grouping through the typed SDK harness."""

    if isinstance(typed_input, list):
        typed_input = GmailPriorityGroupingSDKInput.from_envelopes(typed_input)
    agent = build_gmail_priority_grouping_agent(
        model=model,
        request_text=skill_request_text(typed_input),
    )
    return run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=GmailPriorityGroupingResult,
        run_config=run_config,
        live=live,
        session=session,
    )


def build_gmail_triage_agent(
    model: str | None = None,
    *,
    include_tools: bool = True,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the Gmail triage agent."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "gmail_triage.md",
        skill_files=select_agent_skill_names(
            "gmail_triage",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    tools = (
        [
            get_gmail_message,
            apply_gmail_labels,
            create_gmail_draft_reply,
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
    if tool_tier is not None:
        tools = filter_tools_for_tier("gmail_triage", tools, tool_tier)
    return build_sdk_agent(
        name="gmail_triage",
        instructions=instructions,
        output_type=EmailTriageResult,
        tools=tools,
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="gmail_triage",
        handoff_description=(
            "Use for inbound email classification, label planning, suspicious message review, "
            "and draft-only reply preparation."
        ),
    )


def _default_gmail_triage_sdk_tool_tier(
    typed_input: GmailTriageSDKInput | str,
) -> str:
    """Infer the default Gmail SDK tool tier from the operator request."""

    request_text = (
        typed_input if isinstance(typed_input, str) else getattr(typed_input, "request", "")
    )
    request_text = str(request_text or "").lower()
    if any(marker in request_text for marker in ("draft", "reply", "label", "archive")):
        return "internal_write"
    if any(marker in request_text for marker in ("search web", "source", "research", "look up")):
        return "web_search"
    return "core_read"


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
