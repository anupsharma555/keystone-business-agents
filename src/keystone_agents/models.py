"""Shared lightweight domain models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

from keystone_agents.schemas.email_triage import GmailMessageEnvelope
from keystone_agents.schemas.retrieval import RetrievalHint

TOutput = TypeVar("TOutput")
DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST = (
    "Review my emails from the last 3 days. Group into urgent, important, "
    "can wait, and ignore. Draft replies only for urgent items."
)
GMAIL_PRIORITY_BODY_CHAR_LIMIT = 900
GMAIL_PRIORITY_THREAD_CHAR_LIMIT = 900
GMAIL_PRIORITY_STYLE_CHAR_LIMIT = 1200


def _compact_prompt_text(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return (
        text[:limit].rstrip()
        + "\n[truncated for batch triage; use the message/thread id for full review]"
    )


class RunMode(StrEnum):
    DRY_RUN = "dry-run"
    LIVE = "live"


@dataclass(frozen=True)
class AgentRunRequest:
    """Generic request envelope for dry-run script entrypoints."""

    mode: RunMode = RunMode.DRY_RUN
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    """Generic response envelope for offline scaffolding."""

    agent_name: str
    mode: RunMode
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TypedAgentRunResult(Generic[TOutput]):
    """Typed SDK run result preserving the raw SDK response for inspection."""

    agent_name: str
    output: TOutput
    raw_result: Any
    live: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    budget_guard: dict[str, Any] = field(default_factory=dict)

    @property
    def final_output(self) -> TOutput:
        return self.output


@dataclass(frozen=True)
class GmailTriageSDKInput:
    subject: str
    body: str
    request: str = ""
    sender_name: str = ""
    sender_email: str = ""
    message_id: str = ""
    thread_id: str = ""
    received_at: str = ""
    snippet: str = ""
    prior_labels: list[str] = field(default_factory=list)
    extracted_links: list[dict[str, Any]] = field(default_factory=list)
    attachment_metadata: list[dict[str, Any]] = field(default_factory=list)
    thread_summary: str = ""
    thread_context: str = ""
    suspicious_signals: list[str] = field(default_factory=list)
    triage_limitations: list[str] = field(default_factory=list)
    email_style_profile: str = ""
    founder_fit_context: str = ""

    @classmethod
    def from_envelope(cls, envelope: GmailMessageEnvelope) -> GmailTriageSDKInput:
        """Build sanitized typed input from a normalized Gmail envelope."""

        return cls(
            subject=envelope.subject,
            body=envelope.normalized_body,
            sender_name=envelope.sender_name,
            sender_email=envelope.sender_email,
            message_id=envelope.message_id,
            thread_id=envelope.thread_id,
            received_at=envelope.received_at,
            snippet=envelope.snippet,
            prior_labels=list(envelope.prior_labels),
            extracted_links=[link.model_dump(mode="json") for link in envelope.extracted_links],
            attachment_metadata=[
                attachment.model_dump(mode="json") for attachment in envelope.attachment_metadata
            ],
            thread_summary=envelope.thread_summary,
            thread_context=envelope.thread_context,
            suspicious_signals=list(envelope.suspicious_signals),
            triage_limitations=list(envelope.triage_limitations),
        )

    def to_prompt(self) -> str:
        lines = [
            "Classify this inbound Gmail message using only sanitized context.",
            "Never send email. Drafts must remain draft-only and approval-gated.",
        ]
        if self.request:
            lines.extend(["", "Operator request:", self.request])
        lines.extend(
            [
                f"Message ID: {self.message_id}",
                f"Thread ID: {self.thread_id}",
                f"Received at: {self.received_at}",
                f"From: {self.sender_name} <{self.sender_email}>",
                f"Subject: {self.subject}",
            ]
        )
        if self.snippet:
            lines.append(f"Snippet: {self.snippet}")
        if self.prior_labels:
            lines.append(f"Prior labels: {', '.join(self.prior_labels)}")
        if self.thread_summary or self.thread_context:
            lines.extend(
                [
                    "",
                    "Thread context:",
                    self.thread_context or self.thread_summary,
                ]
            )
        if self.extracted_links:
            lines.extend(["", "Extracted links:"])
            for link in self.extracted_links:
                reasons = ", ".join(str(item) for item in link.get("reasons", []))
                lines.append(
                    "- "
                    f"{link.get('url', '')} "
                    f"(domain={link.get('domain', '')}, "
                    f"suspicious={bool(link.get('suspicious'))}, "
                    f"reasons={reasons or 'none'})"
                )
        if self.attachment_metadata:
            lines.extend(["", "Attachment metadata only, no attachment bodies were ingested:"])
            for attachment in self.attachment_metadata:
                flags = ", ".join(str(item) for item in attachment.get("risk_flags", []))
                lines.append(
                    "- "
                    f"{attachment.get('filename', '') or '(unnamed)'} "
                    f"({attachment.get('mime_type', '')}, "
                    f"{attachment.get('size_bytes', 0)} bytes, "
                    f"risk_flags={flags or 'none'})"
                )
        if self.suspicious_signals:
            lines.extend(["", "Pre-screened suspicious signals:", *self.suspicious_signals])
        if self.triage_limitations:
            lines.extend(["", "Triage limitations:", *self.triage_limitations])
        if self.email_style_profile:
            lines.extend(
                [
                    "",
                    "Optional approved aggregate email style profile:",
                    self.email_style_profile,
                ]
            )
        if self.founder_fit_context:
            lines.extend(
                [
                    "",
                    "Optional approved founder context for business-fit and reply drafting:",
                    self.founder_fit_context,
                ]
            )
        lines.extend(["", "Normalized body:", self.body])
        return "\n".join(line for line in lines if line is not None).strip()


@dataclass(frozen=True)
class GmailPriorityGroupingSDKInput:
    """Typed SDK input for GT-1 batch Gmail priority grouping."""

    messages: list[GmailTriageSDKInput]
    request: str = DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST
    lookback_days: int = 3
    source_label: str = "UNREAD"
    draft_policy: str = (
        "Draft replies may appear only for urgent messages, must remain draft-only, "
        "and must require human approval. Never send."
    )

    @classmethod
    def from_envelopes(
        cls,
        envelopes: list[GmailMessageEnvelope],
        *,
        request: str | None = None,
        lookback_days: int = 3,
        source_label: str = "UNREAD",
        email_style_profile: str = "",
        founder_fit_context: str = "",
    ) -> GmailPriorityGroupingSDKInput:
        """Build sanitized batch input from normalized Gmail envelopes."""

        messages: list[GmailTriageSDKInput] = []
        for envelope in envelopes:
            typed_input = GmailTriageSDKInput.from_envelope(envelope)
            if email_style_profile or founder_fit_context:
                typed_input = GmailTriageSDKInput(
                    subject=typed_input.subject,
                    body=typed_input.body,
                    sender_name=typed_input.sender_name,
                    sender_email=typed_input.sender_email,
                    message_id=typed_input.message_id,
                    thread_id=typed_input.thread_id,
                    received_at=typed_input.received_at,
                    snippet=typed_input.snippet,
                    prior_labels=list(typed_input.prior_labels),
                    extracted_links=list(typed_input.extracted_links),
                    attachment_metadata=list(typed_input.attachment_metadata),
                    thread_summary=typed_input.thread_summary,
                    thread_context=typed_input.thread_context,
                    suspicious_signals=list(typed_input.suspicious_signals),
                    triage_limitations=list(typed_input.triage_limitations),
                    email_style_profile=email_style_profile,
                    founder_fit_context=founder_fit_context,
                )
            messages.append(typed_input)
        return cls(
            messages=messages,
            request=request or DEFAULT_GMAIL_PRIORITY_GROUPING_REQUEST,
            lookback_days=lookback_days,
            source_label=source_label,
        )

    def to_prompt(self) -> str:
        lines = [
            "Complete the Gmail priority grouping request using only sanitized inputs.",
            self.request,
            "",
            "Required buckets: urgent, important, can_wait, ignore.",
            "Classify each source message into exactly one bucket.",
            self.draft_policy,
            "Do not include draft_reply for important, can_wait, or ignore messages.",
            "Do not create Gmail drafts, send email, apply labels, or call live integrations.",
            "Set send_enabled=false, sent=false, and live_side_effects_enabled=false.",
            f"Source label: {self.source_label}",
            f"Lookback days: {self.lookback_days}",
            f"Source message count: {len(self.messages)}",
        ]
        for index, message in enumerate(self.messages, start=1):
            lines.extend(
                [
                    "",
                    f"Message {index}:",
                    f"Message ID: {message.message_id}",
                    f"Thread ID: {message.thread_id}",
                    f"Received at: {message.received_at}",
                    f"From: {message.sender_name} <{message.sender_email}>",
                    f"Subject: {message.subject}",
                    f"Snippet: {message.snippet}",
                ]
            )
            if message.prior_labels:
                lines.append(f"Prior labels: {', '.join(message.prior_labels)}")
            if message.thread_summary or message.thread_context:
                lines.extend(
                    [
                        "Thread context:",
                        _compact_prompt_text(
                            message.thread_context or message.thread_summary,
                            limit=GMAIL_PRIORITY_THREAD_CHAR_LIMIT,
                        ),
                    ]
                )
            if message.extracted_links:
                lines.append("Extracted links:")
                for link in message.extracted_links:
                    reasons = ", ".join(str(item) for item in link.get("reasons", []))
                    lines.append(
                        "- "
                        f"{link.get('url', '')} "
                        f"(domain={link.get('domain', '')}, "
                        f"suspicious={bool(link.get('suspicious'))}, "
                        f"reasons={reasons or 'none'})"
                    )
            if message.attachment_metadata:
                lines.append("Attachment metadata only, no attachment bodies were ingested:")
                for attachment in message.attachment_metadata:
                    flags = ", ".join(str(item) for item in attachment.get("risk_flags", []))
                    lines.append(
                        "- "
                        f"{attachment.get('filename', '') or '(unnamed)'} "
                        f"({attachment.get('mime_type', '')}, "
                        f"{attachment.get('size_bytes', 0)} bytes, "
                        f"risk_flags={flags or 'none'})"
                    )
            if message.suspicious_signals:
                lines.extend(["Pre-screened suspicious signals:", *message.suspicious_signals])
            if message.triage_limitations:
                lines.extend(["Triage limitations:", *message.triage_limitations])
            if message.email_style_profile:
                lines.extend(
                    [
                        "Optional approved aggregate email style profile:",
                        _compact_prompt_text(
                            message.email_style_profile,
                            limit=GMAIL_PRIORITY_STYLE_CHAR_LIMIT,
                        ),
                    ]
                )
            if message.founder_fit_context:
                lines.extend(
                    [
                        "Optional approved founder context for business-fit and reply drafting:",
                        _compact_prompt_text(
                            message.founder_fit_context,
                            limit=GMAIL_PRIORITY_STYLE_CHAR_LIMIT,
                        ),
                    ]
                )
            lines.extend(
                [
                    "Normalized body:",
                    _compact_prompt_text(message.body, limit=GMAIL_PRIORITY_BODY_CHAR_LIMIT),
                ]
            )
        return "\n".join(line for line in lines if line is not None).strip()


@dataclass(frozen=True)
class BusinessResearchSDKInput:
    company_name: str
    company_url: str | None = None
    lead_name: str | None = None
    linkedin_url: str | None = None
    context: str = ""
    retrieval_hint: RetrievalHint | None = None

    def to_prompt(self) -> str:
        lines = [
            "Research this company using only approved context and source-linked tool results.",
            f"Company: {self.company_name}",
        ]
        if self.company_url:
            lines.append(f"Company URL: {self.company_url}")
        if self.lead_name:
            lines.append(f"Lead/contact: {self.lead_name}")
        if self.linkedin_url:
            lines.append(f"LinkedIn/profile URL: {self.linkedin_url}")
        if self.retrieval_hint is not None:
            lines.extend(
                [
                    "",
                    "Optional retrieval guidance from the control plane "
                    "(recommendation only; deterministic quality gates still decide escalation):",
                    json.dumps(
                        self.retrieval_hint.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
        if self.context:
            lines.extend(["", self.context])
        return "\n".join(lines)


@dataclass(frozen=True)
class BusinessResearchFocusedBriefSDKInput:
    company_name: str
    company_url: str | None = None
    source_context: str = ""
    retrieval_hint: RetrievalHint | None = None
    brief_goal: str = (
        "Prepare a concise research brief for possible Keystone partnership or advisory relevance."
    )

    def to_prompt(self) -> str:
        lines = [
            "Produce the Business Research Analyst BR-1 focused brief.",
            self.brief_goal,
            "Use only the approved source-backed company profile context below.",
            "Do not invent facts, buyer segments, traction, leadership, funding, metrics, "
            "or product status.",
            "Put factual claims in facts[] with source_ids. Put judgment calls only in "
            "inferences[].",
            "If leadership is not source-backed, set leadership to unknown and list it in "
            "unknowns.",
            "Keep the brief concise and decision-oriented.",
            f"Company: {self.company_name}",
        ]
        if self.company_url:
            lines.append(f"Company URL: {self.company_url}")
        if self.retrieval_hint is not None:
            lines.extend(
                [
                    "",
                    "Optional retrieval guidance captured for this run:",
                    json.dumps(
                        self.retrieval_hint.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
        if self.source_context:
            lines.extend(["", "Approved source-backed context:", self.source_context])
        return "\n".join(lines)


@dataclass(frozen=True)
class BusinessResearchComparisonSDKInput:
    company_a: str
    company_b: str
    decision_goal: str = ""
    decision_criteria: tuple[str, ...] = ()
    requested_output_format: str | None = None
    source_context: str = ""
    retrieval_hint: RetrievalHint | None = None

    def to_prompt(self) -> str:
        lines = [
            "Produce a source-backed Business Research Analyst company comparison.",
            "Use the structured comparison context and company profiles below as evidence.",
            "Synthesize a concise decision memo inside the CompanyResearchComparison schema.",
            "Keep judgment flexible, but cite source_ids in each side-by-side entry "
            "where facts are used.",
            "Distinguish source-backed facts from inferences, and keep unknowns visible.",
            "Do not invent websites, contacts, buyer personas, funding, outcomes, or "
            "Keystone history.",
            f"Company A: {self.company_a}",
            f"Company B: {self.company_b}",
        ]
        if self.decision_goal:
            lines.append(f"Decision goal: {self.decision_goal}")
        if self.decision_criteria:
            lines.append("Decision criteria: " + ", ".join(self.decision_criteria))
        if self.requested_output_format:
            lines.append(f"Requested human format: {self.requested_output_format}")
        if self.retrieval_hint is not None:
            lines.extend(
                [
                    "",
                    "Optional retrieval guidance captured for this run:",
                    json.dumps(
                        self.retrieval_hint.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
        if self.source_context:
            lines.extend(["", "Approved source-backed comparison context:", self.source_context])
        return "\n".join(lines)


@dataclass(frozen=True)
class ResearchSDKInput:
    target_name: str
    target_type: str = "other"
    research_goal: str = "Prepare a concise source-backed research brief for Keystone review."
    source_context: str = ""
    local_context_source_ids: tuple[str, ...] = ()
    retrieval_hint: RetrievalHint | None = None

    def to_prompt(self) -> str:
        lines = [
            "Produce a Business Research Analyst source-backed brief.",
            self.research_goal,
            "The target may be a company, institute, conference, lab, person, topic, "
            "Zotero article, Zotero collection, or article collection.",
            "Use only approved context, local-context snippets, and source-linked tool results.",
            "For Zotero article or collection requests, summarize source-level questions, "
            "methods, findings, limitations, and relevance to the requested goal when "
            "the context supports it.",
            "Put factual claims in facts[] with source_ids. Put synthesis or Keystone "
            "relevance judgments only in inferences[].",
            "If evidence is missing or only local/private context is available, state the "
            "limitation explicitly in unknowns or limitations.",
            "Do not invent authors, venues, findings, dates, affiliations, funding, "
            "leadership, metrics, or publication status.",
            "Never enable sends or include raw full source content.",
            f"Target: {self.target_name}",
            f"Target type: {self.target_type}",
        ]
        if self.local_context_source_ids:
            lines.append(
                "Suggested local context source IDs: " + ", ".join(self.local_context_source_ids)
            )
        if self.retrieval_hint is not None:
            lines.extend(
                [
                    "",
                    "Optional retrieval guidance captured for this run:",
                    json.dumps(
                        self.retrieval_hint.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
        if self.source_context:
            lines.extend(["", "Approved source-backed context:", self.source_context])
        return "\n".join(lines)


@dataclass(frozen=True)
class OpportunityScoutSDKInput:
    topic: str | None = None
    max_results: int = 5
    context: str = ""
    retrieval_hint: RetrievalHint | None = None

    def to_prompt(self) -> str:
        lines = [
            "Scout source-backed Keystone business opportunities.",
            f"Topic: {self.topic or 'general Keystone target domains'}",
            f"Maximum records: {self.max_results}",
        ]
        if self.retrieval_hint is not None:
            lines.extend(
                [
                    "",
                    "Optional retrieval guidance from the control plane "
                    "(recommendation only; deterministic quality gates still decide escalation):",
                    json.dumps(
                        self.retrieval_hint.model_dump(mode="json"),
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                ]
            )
        if self.context:
            lines.extend(["", self.context])
        return "\n".join(lines)


@dataclass(frozen=True)
class OutreachComposerSDKInput:
    company_name: str
    contact_name: str | None = None
    contact_title: str | None = None
    recent_signal: str | None = None
    outreach_goal: str | None = None
    approved_context: str = ""
    email_style_profile: str = ""
    outreach_template: str = ""
    example_guidance: str = ""

    def to_prompt(self) -> str:
        lines = [
            (
                "Draft approval-gated outreach using only the approved company, "
                "research brief, opportunity, contact, CRM, style, template, and "
                "example context explicitly supplied below."
            ),
            f"Company: {self.company_name}",
        ]
        if self.contact_name:
            lines.append(f"Contact name: {self.contact_name}")
        if self.contact_title:
            lines.append(f"Contact title: {self.contact_title}")
            lines.append(
                "Use this contact title as the recipient persona. Preserve it in the "
                "draft metadata when the output schema supports it."
            )
        if self.recent_signal:
            lines.append(f"Recent signal: {self.recent_signal}")
        if self.outreach_goal:
            lines.append(f"Goal: {self.outreach_goal}")
        lines.append(
            "End with exactly one clear, low-pressure CTA question. Prefer wording like "
            "'Would a brief exploratory conversation be useful?' or 'Open to compare notes?'"
        )
        if self.approved_context:
            lines.extend(["", self.approved_context])
        if self.email_style_profile:
            lines.extend(
                [
                    "",
                    "Optional approved aggregate email style profile:",
                    self.email_style_profile,
                ]
            )
        if self.outreach_template:
            lines.extend(
                [
                    "",
                    "Optional selected outreach template. Use it for structure, pacing, "
                    "CTA, and follow-up pattern only. It provides no factual claims about "
                    "the current prospect:",
                    self.outreach_template,
                ]
            )
        if self.example_guidance:
            lines.extend(
                [
                    "",
                    "Optional approved RAG example guidance. Examples guide tone, "
                    "structure, pacing, CTA, and follow-up pattern only. They do not "
                    "provide factual claims about the current prospect:",
                    self.example_guidance,
                ]
            )
        return "\n".join(lines)
