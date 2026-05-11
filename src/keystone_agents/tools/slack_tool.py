"""Safe Slack notification boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from keystone_agents.config import load_settings
from keystone_agents.guardrails import (
    assess_tool_payload_guardrails,
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
)
from keystone_agents.schemas.approval import ApprovalQueueItem, ApprovalRequest
from keystone_agents.schemas.review_card import ReviewCard, ReviewEvidenceItem, ReviewSourceItem
from keystone_agents.storage.sqlite_store import redact_secrets


class SlackConfigurationError(RuntimeError):
    """Raised when a live Slack call is requested without required credentials."""


_OMIT_FROM_SLACK_FLAGS = frozenset({"possible_phi", "secret", "security", "professional_advice"})
_REVIEW_OBJECT_TYPES = {
    "gmail_triage",
    "gmail_draft",
    "company_profile",
    "opportunity",
    "outreach_draft",
    "pipeline",
    "approval",
    "other",
}
_HUMAN_LABELS = {
    "gmail_triage": "Gmail triage",
    "gmail_draft": "Gmail draft",
    "company_profile": "company profile",
    "opportunity": "opportunity",
    "outreach_draft": "outreach draft",
    "pipeline": "pipeline",
    "approval": "approval",
    "external_use": "external use",
    "external_copy": "external copy",
    "possible_phi": "possible PHI",
}


@dataclass(frozen=True)
class SlackReviewMessage:
    """Slack-ready review copy with root text and thread-ready detail blocks."""

    root_text: str
    thread_blocks: tuple[str, ...] = field(default_factory=tuple)
    approval_item_id: str = ""
    object_type: str = ""
    status: str = ""
    scope: str = ""
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    next_safe_action: str = ""
    send_enabled: bool = False
    interactive_actions_enabled: bool = False

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for local tests, logs, or future posting."""

        return {
            "root_text": self.root_text,
            "thread_blocks": list(self.thread_blocks),
            "approval_item_id": self.approval_item_id,
            "object_type": self.object_type,
            "status": self.status,
            "scope": self.scope,
            "risk_flags": list(self.risk_flags),
            "next_safe_action": self.next_safe_action,
            "send_enabled": self.send_enabled,
            "interactive_actions_enabled": self.interactive_actions_enabled,
        }


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _clean_slack_text(value: Any) -> str:
    redacted = redact_secrets(value)
    return str(redacted if redacted is not None else "").replace("\u2014", "-").strip()


def _compact_text(value: Any, *, max_chars: int = 220) -> str:
    text = " ".join(_clean_slack_text(value).split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _sensitive_flags(value: str) -> set[str]:
    assessment = assess_tool_payload_guardrails("slack_review_message_format", value)
    return set(assessment.risk_flags)


def _public_text(value: Any, *, max_chars: int = 220) -> str:
    text = _compact_text(value, max_chars=max_chars)
    if not text:
        return ""
    if _sensitive_flags(text) & _OMIT_FROM_SLACK_FLAGS:
        return "[omitted: sensitive content flagged]"
    return text


def _thread_text(value: Any, *, max_chars: int = 2400) -> str:
    text = _clean_slack_text(value)
    if not text:
        return ""
    flags = _sensitive_flags(text)
    if flags & _OMIT_FROM_SLACK_FLAGS:
        return "[omitted: sensitive content flagged for Slack review]"
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _join_flags(values: Sequence[Any] | None) -> str:
    flags = [_compact_text(value, max_chars=80) for value in values or []]
    flags = [flag for flag in flags if flag]
    return ", ".join(dict.fromkeys(flags)) if flags else "none"


def _human_label(value: Any) -> str:
    text = _compact_text(value, max_chars=120)
    if not text:
        return ""
    return _HUMAN_LABELS.get(text.lower(), text.replace("_", " ").replace("-", " "))


def _join_human_labels(values: Sequence[Any] | None) -> str:
    labels = [_human_label(value) for value in values or []]
    labels = [label for label in labels if label]
    return ", ".join(dict.fromkeys(labels)) if labels else "none"


def _thread_block(title: str, rows: Sequence[str]) -> str:
    body = "\n".join(row for row in rows if row.strip()) or "- none"
    return f"{title}\n{body}"


def _evidence_rows(evidence: Sequence[ReviewEvidenceItem]) -> list[str]:
    rows: list[str] = []
    for index, item in enumerate(evidence, start=1):
        text = _public_text(item.text, max_chars=260)
        if not text:
            continue
        details = []
        if item.confidence:
            details.append(f"confidence: {_compact_text(item.confidence, max_chars=60)}")
        suffix = f" ({', '.join(details)})" if details else ""
        rows.append(f"{index}. {text}{suffix}")
    return rows


def _source_rows(sources: Sequence[ReviewSourceItem]) -> list[str]:
    rows: list[str] = []
    for source in sources:
        label = (
            _compact_text(source.title, max_chars=120)
            or _compact_text(source.source_id, max_chars=120)
            or "source"
        )
        url = _compact_text(source.url, max_chars=240)
        rows.append(f"- {label}: {url}" if url else f"- {label}")
    return rows


def _review_object_type(value: Any) -> str:
    object_type = _enum_value(value).lower()
    return object_type if object_type in _REVIEW_OBJECT_TYPES else "other"


def _scope_for_queue_item(data: dict[str, Any]) -> str:
    metadata = _as_dict(data.get("metadata"))
    explicit = _enum_value(metadata.get("approval_scope") or metadata.get("scope"))
    if explicit:
        return explicit
    object_type = _enum_value(data.get("object_type"))
    if object_type == "outreach_draft":
        return "external_use"
    if object_type in {"company_profile", "opportunity"}:
        return "drafting"
    if object_type == "gmail_draft":
        return "send"
    return "review"


def _next_safe_action(status: str, *, fallback: str = "") -> str:
    if fallback:
        return fallback
    if status == "pending":
        return "Review the thread details, then update the local approval queue outside Slack."
    if status == "revise":
        return "Revise the draft locally, then resubmit for review."
    if status == "approved":
        return "Approved for the recorded scope only. Slack still does not send or publish."
    if status in {"rejected", "expired", "archived"}:
        return "No outbound action. Keep the item closed unless a new review is created."
    return "Review locally before any external use."


def _slack_action_blocks(message: SlackReviewMessage) -> list[dict[str, Any]]:
    if not message.approval_item_id or message.approval_item_id == "not saved":
        return []
    return [
        {
            "type": "actions",
            "block_id": "keystone_approval_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Yes"},
                    "style": "primary",
                    "action_id": "keystone_approval_yes",
                    "value": message.approval_item_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Needs edits"},
                    "action_id": "keystone_approval_revise",
                    "value": message.approval_item_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "No"},
                    "style": "danger",
                    "action_id": "keystone_approval_no",
                    "value": message.approval_item_id,
                },
            ],
        }
    ]


def _evidence_from_metadata(metadata: dict[str, Any]) -> list[ReviewEvidenceItem]:
    rows: list[ReviewEvidenceItem] = []
    for raw in metadata.get("evidence") or metadata.get("facts_used") or []:
        data = _as_dict(raw)
        if data:
            text = data.get("text") or data.get("claim_text") or data.get("claim")
            source_id = data.get("source_id") or data.get("source")
            confidence = data.get("confidence")
        else:
            text = raw
            source_id = ""
            confidence = ""
        cleaned = _public_text(text, max_chars=300)
        if cleaned:
            rows.append(
                ReviewEvidenceItem(
                    text=cleaned,
                    source_id=_compact_text(source_id, max_chars=100),
                    confidence=_compact_text(confidence, max_chars=80),
                )
            )
    return rows[:5]


def _sources_from_metadata(metadata: dict[str, Any]) -> list[ReviewSourceItem]:
    sources: list[ReviewSourceItem] = []
    for raw in metadata.get("sources") or []:
        data = _as_dict(raw)
        if not data:
            continue
        sources.append(
            ReviewSourceItem(
                source_id=data.get("source_id") or data.get("id") or "",
                title=data.get("title") or data.get("name") or "",
                url=data.get("url") or data.get("source") or "",
            )
        )
    for raw in metadata.get("source_ids") or []:
        source_id = _compact_text(raw, max_chars=120)
        if source_id and all(source.source_id != source_id for source in sources):
            sources.append(ReviewSourceItem(source_id=source_id, title=source_id))
    return sources[:6]


def _contact_review_rows(metadata: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    company = _public_text(metadata.get("company_name"), max_chars=140)
    website = _public_text(metadata.get("company_website"), max_chars=220)
    contact = _public_text(metadata.get("contact_name"), max_chars=140)
    title = _public_text(metadata.get("contact_title"), max_chars=140)
    email = _public_text(metadata.get("recipient_email"), max_chars=180)
    linkedin_url = _public_text(
        metadata.get("contact_linkedin_url") or metadata.get("linkedin_url"),
        max_chars=220,
    )
    channel = _compact_text(metadata.get("outreach_channel"), max_chars=40).lower()
    if company:
        rows.append(f"Company: {company}")
    if website:
        rows.append(f"Company link: {website}")
    if contact:
        rows.append(f"Contact: {contact}")
    if title:
        rows.append(f"Contact title: {title}")
    if channel == "linkedin":
        rows.append(f"LinkedIn: {linkedin_url or 'Needs confirmation'}")
    elif channel == "email":
        rows.append(f"Email: {email or 'Needs confirmation'}")
    else:
        if email:
            rows.append(f"Email: {email}")
        if linkedin_url:
            rows.append(f"LinkedIn: {linkedin_url}")
    return rows


def _company_research_rows(metadata: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    company = _public_text(metadata.get("company_name"), max_chars=140)
    website = _public_text(metadata.get("company_website"), max_chars=220)
    summary = _public_text(
        metadata.get("company_fit_summary") or metadata.get("company_description"),
        max_chars=520,
    )
    if company:
        rows.append(f"Company: {company}")
    if website:
        rows.append(f"Company link: {website}")
    if summary:
        rows.append(f"CR summary: {summary}")
    scores = _as_dict(metadata.get("company_scores"))
    score_parts = [
        f"consulting fit {scores.get('consulting_fit')}/100"
        if scores.get("consulting_fit") is not None
        else "",
        f"clinical AI {scores.get('clinical_ai')}/100"
        if scores.get("clinical_ai") is not None
        else "",
        f"behavioral health {scores.get('behavioral_health')}/100"
        if scores.get("behavioral_health") is not None
        else "",
        f"evidence generation {scores.get('evidence_generation')}/100"
        if scores.get("evidence_generation") is not None
        else "",
        f"outside consulting {scores.get('outside_consulting')}/100"
        if scores.get("outside_consulting") is not None
        else "",
    ]
    score_text = ", ".join(part for part in score_parts if part)
    if score_text:
        rows.append(f"CR scores: {score_text}")
    source_quality = _as_dict(metadata.get("company_source_quality"))
    if source_quality.get("overall_score") is not None:
        rows.append(
            "Source quality: "
            f"{source_quality.get('overall_score')}/100; "
            f"independent sources {source_quality.get('independent_source_count', 0)}; "
            f"high-quality sources {source_quality.get('high_quality_source_count', 0)}"
        )
    completeness = _as_dict(metadata.get("company_research_completeness"))
    if completeness.get("score") is not None:
        rows.append(f"Research completeness: {completeness.get('score')}/100")
    research_points = []
    for raw in metadata.get("company_research_points") or []:
        point = _as_dict(raw)
        label = _public_text(point.get("label"), max_chars=120)
        value = _public_text(point.get("value"), max_chars=260)
        source_ids = ", ".join(
            _compact_text(source_id, max_chars=80)
            for source_id in (point.get("source_ids") or [])
            if _compact_text(source_id, max_chars=80)
        )
        if label and value:
            suffix = f" [sources: {source_ids}]" if source_ids else ""
            research_points.append(f"- {label}: {value}{suffix}")
    if research_points:
        rows.append("CR facts:")
        rows.extend(research_points[:4])
    claim_rows = []
    for raw in metadata.get("company_claims") or []:
        claim = _as_dict(raw)
        text = _public_text(claim.get("text"), max_chars=260)
        source_id = _public_text(claim.get("source_id"), max_chars=100)
        if text:
            suffix = f" [source: {source_id}]" if source_id else ""
            claim_rows.append(f"- {text}{suffix}")
    if claim_rows:
        rows.append("Source-backed claims:")
        rows.extend(claim_rows[:4])
    gaps = [
        _public_text(gap, max_chars=180)
        for gap in (metadata.get("company_missing_information") or [])
    ]
    gaps = [gap for gap in gaps if gap]
    if gaps:
        rows.append("CR gaps:")
        rows.extend(f"- {gap}" for gap in gaps[:6])
    risks = [_public_text(risk, max_chars=180) for risk in (metadata.get("company_risks") or [])]
    risks = [risk for risk in risks if risk]
    if risks:
        rows.append("CR risks:")
        rows.extend(f"- {risk}" for risk in risks[:4])
    source_rows = _source_rows(_sources_from_metadata(metadata))
    if source_rows:
        rows.append("Source links:")
        rows.extend(source_rows[:6])
    return rows


def _draft_review_rows(
    metadata: dict[str, Any],
    draft_text: Any,
    *,
    max_thread_chars: int,
) -> list[str]:
    rows: list[str] = []
    channel = _compact_text(metadata.get("outreach_channel"), max_chars=40).lower()
    channel_label = "LinkedIn" if channel == "linkedin" else "Email"
    company = _public_text(metadata.get("company_name"), max_chars=140)
    contact = _public_text(metadata.get("contact_name"), max_chars=140)
    email = _public_text(metadata.get("recipient_email"), max_chars=180)
    linkedin_url = _public_text(
        metadata.get("contact_linkedin_url") or metadata.get("linkedin_url"),
        max_chars=220,
    )
    destination = linkedin_url if channel == "linkedin" else email
    rows.append(f"Channel: {channel_label}")
    if company:
        rows.append(f"Company: {company}")
    if contact:
        rows.append(f"Contact: {contact}")
    rows.append(f"Destination: {destination or 'Needs confirmation'}")
    blockers = [
        _public_text(blocker, max_chars=180)
        for blocker in (metadata.get("missing_information_blockers") or [])
    ]
    blockers = [blocker for blocker in blockers if blocker]
    if blockers:
        rows.append("Needs confirmation:")
        rows.extend(f"- {blocker}" for blocker in blockers[:5])
    rows.append("Draft text:")
    rows.append(_thread_text(draft_text, max_chars=max_thread_chars) or "- none")
    rows.append("Draft only. Human approval is required before any external use.")
    return rows


def format_slack_review_message(
    card: ReviewCard | Mapping[str, Any],
    *,
    approval_item_id: str = "",
    object_type: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    risk_flags: Sequence[str] | None = None,
    next_safe_action: str | None = None,
    draft_text: str | None = None,
    max_thread_chars: int = 2400,
) -> SlackReviewMessage:
    """Format one approval review for Slack without approving, rejecting, or sending."""

    data = card if isinstance(card, ReviewCard) else ReviewCard.model_validate(card)
    resolved_id = _compact_text(approval_item_id or data.object_id or "not saved", max_chars=120)
    resolved_type = _review_object_type(object_type or data.object_type)
    resolved_status = _compact_text(status or data.approval_status or "pending", max_chars=80)
    resolved_scope = _compact_text(scope or data.approval_scope or "review", max_chars=80)
    resolved_risks = tuple(
        dict.fromkeys(_compact_text(flag, max_chars=80) for flag in (risk_flags or data.risks))
    )
    resolved_risks = tuple(flag for flag in resolved_risks if flag)
    resolved_next = _public_text(
        next_safe_action or data.next_action,
        max_chars=260,
    )
    if not resolved_next:
        resolved_next = _next_safe_action(resolved_status)
    display_title = _public_text(data.title, max_chars=140) or _human_label(resolved_type)

    lowered_title = display_title.lower()
    if "linkedin" in lowered_title:
        channel = "LinkedIn"
    elif "email" in lowered_title:
        channel = "email"
    else:
        channel = _human_label(resolved_type)
    root_sections = [
        "Keystone Business Agents Workflow review",
        f"Review: {display_title}",
        f"Approval ID: {resolved_id}",
        (
            f"Type: {_human_label(resolved_type)} | Channel: {channel} | "
            f"Status: {_human_label(resolved_status)} | Scope: {_human_label(resolved_scope)} | "
            f"Risk flags: {_join_human_labels(resolved_risks)}"
        ),
        f"Decision: {_public_text(data.decision_summary, max_chars=220)}",
        (
            f"Approval question: Yes or No - approve this {channel} draft for "
            f"{_human_label(resolved_scope)}?"
        ),
        (
            "Feedback: Yes creates a Gmail draft for email approvals only. For Needs edits, "
            "reply in thread with edit instructions for the LLM. For No, reply with why."
        ),
        f"Next safe action: {resolved_next}",
        "Slack buttons update only the local approval queue. They do not send email.",
    ]
    review_rows = []
    evidence_rows = _evidence_rows(data.evidence)
    if evidence_rows:
        review_rows.append("Evidence:")
        review_rows.extend(evidence_rows)
    reason = _public_text(data.reason, max_chars=420)
    if reason:
        review_rows.append(f"Review rationale: {reason}")
    source_rows = _source_rows(data.sources)
    if source_rows:
        review_rows.append("Sources:")
        review_rows.extend(source_rows)
    if data.outbound_copy:
        review_rows.append(
            _public_text(
                data.approval_warning,
                max_chars=260,
            )
            or "Draft only. Human approval required before outbound communication."
        )
    thread_blocks = [_thread_block("Review details", review_rows)]
    if draft_text is not None:
        thread_blocks.append(
            _thread_block(
                "Draft text",
                [_thread_text(draft_text, max_chars=max_thread_chars) or "- none"],
            )
        )

    message = SlackReviewMessage(
        root_text="\n\n".join(section for section in root_sections if _clean_slack_text(section)),
        thread_blocks=tuple(block for block in thread_blocks if _clean_slack_text(block)),
        approval_item_id=resolved_id,
        object_type=resolved_type,
        status=resolved_status,
        scope=resolved_scope,
        risk_flags=resolved_risks,
        next_safe_action=resolved_next,
        interactive_actions_enabled=bool(resolved_id and resolved_id != "not saved"),
    )
    return message


def slack_review_message_from_approval_item(
    item: ApprovalQueueItem | Mapping[str, Any],
    *,
    max_thread_chars: int = 2400,
) -> SlackReviewMessage:
    """Build Slack review copy from a local approval queue item."""

    data = _as_dict(item)
    metadata = _as_dict(data.get("metadata"))
    status = _enum_value(data.get("approval_status")) or "pending"
    object_type = _enum_value(data.get("object_type")) or "other"
    card = ReviewCard(
        title=_public_text(data.get("title"), max_chars=160) or "Approval review",
        object_type=_review_object_type(object_type),
        object_id=_enum_value(data.get("object_id")) or _enum_value(data.get("id")),
        decision_summary=_public_text(data.get("summary"), max_chars=240)
        or "Approval review requested.",
        reason=_public_text(data.get("summary"), max_chars=420)
        or "Human review is required before external use.",
        evidence=_evidence_from_metadata(metadata),
        risks=data.get("risk_flags") or [],
        approval_required=True,
        approval_status=status,
        approval_scope=_scope_for_queue_item(data),
        next_action=_next_safe_action(
            status,
            fallback=_public_text(metadata.get("next_safe_action"), max_chars=260),
        ),
        sources=_sources_from_metadata(metadata),
        outbound_copy=bool(data.get("draft_text")),
    )
    message = format_slack_review_message(
        card,
        approval_item_id=_enum_value(data.get("id")),
        object_type=object_type,
        status=status,
        scope=card.approval_scope,
        risk_flags=data.get("risk_flags") or [],
        draft_text=data.get("draft_text"),
        max_thread_chars=max_thread_chars,
    )
    root_sections = message.root_text.split("\n\n")
    root_sections.insert(3, "\n".join(_contact_review_rows(metadata)))
    thread_blocks = tuple(message.thread_blocks)
    if metadata.get("company_name") and data.get("draft_text"):
        thread_blocks = (
            _thread_block("Company research", _company_research_rows(metadata)),
            _thread_block(
                "Draft for approval",
                _draft_review_rows(
                    metadata,
                    data.get("draft_text"),
                    max_thread_chars=max_thread_chars,
                ),
            ),
        )
    return SlackReviewMessage(
        **{
            **message.as_payload(),
            "root_text": "\n\n".join(section for section in root_sections if section.strip()),
            "thread_blocks": thread_blocks,
            "risk_flags": tuple(message.risk_flags),
        }
    )


def slack_review_message_from_approval_request(
    request: ApprovalRequest | Mapping[str, Any],
    *,
    approval_item_id: str = "",
    max_thread_chars: int = 2400,
) -> SlackReviewMessage:
    """Build Slack review copy from a draft-only approval request."""

    data = _as_dict(request)
    status = _enum_value(data.get("decision")) or "pending"
    object_type = _enum_value(data.get("object_type")) or "approval"
    card = ReviewCard(
        title=f"{_public_text(object_type, max_chars=80) or 'approval'} review",
        object_type=_review_object_type(object_type),
        object_id=_enum_value(data.get("object_id")),
        decision_summary=_public_text(data.get("summary"), max_chars=240)
        or "Approval review requested.",
        reason="Draft-only approval request queued for human review.",
        risks=data.get("risk_flags") or [],
        approval_required=True,
        approval_status=status,
        approval_scope=_enum_value(data.get("scope")) or "review",
        next_action=_next_safe_action(status),
        outbound_copy=bool(data.get("draft_text")),
    )
    return format_slack_review_message(
        card,
        approval_item_id=approval_item_id or _enum_value(data.get("object_id")),
        object_type=object_type,
        status=status,
        scope=card.approval_scope,
        risk_flags=data.get("risk_flags") or [],
        draft_text=data.get("draft_text") or "",
        max_thread_chars=max_thread_chars,
    )


@dataclass(frozen=True)
class SlackTool:
    """Post approval notifications to Slack only when live mode is explicit."""

    live: bool = False
    bot_token: str | None = None
    approvals_channel: str | None = None

    def _resolved_credentials(self) -> tuple[str | None, str | None]:
        settings = load_settings()
        return (
            self.bot_token or settings.slack_bot_token,
            self.approvals_channel or settings.slack_channel_approvals,
        )

    def post_message(self, channel: str | None, text: str) -> dict[str, Any]:
        enforce_tool_input_guardrails(
            "slack_post_message",
            {"channel": channel, "text": text, "live": self.live},
        )
        if not self.live:
            _token, default_channel = self._resolved_credentials()
            resolved_channel = channel or default_channel
            return enforce_tool_output_guardrails(
                "slack_post_message",
                {
                    "status": "dry-run",
                    "channel": resolved_channel or "dry-run-approvals",
                    "text_preview": text[:120],
                    "ts": "dry-run-slack-ts",
                },
            )

        output = self._post_message_payload(channel=channel, text=text)
        return enforce_tool_output_guardrails("slack_post_message", output)

    def post_review_message(
        self,
        message: SlackReviewMessage,
        *,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Post a compact review root and thread detail blocks."""

        if not self.live:
            return enforce_tool_output_guardrails(
                "slack_post_review_message",
                {
                    "status": "dry-run",
                    "channel": channel or self.approvals_channel or "dry-run-approvals",
                    "ts": "dry-run-slack-ts",
                    "thread_blocks_posted": len(message.thread_blocks),
                    "send_enabled": False,
                },
            )

        root = self._post_message_payload(
            channel=channel,
            text=message.root_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message.root_text},
                },
                *_slack_action_blocks(message),
            ],
        )
        thread_ts = str(root.get("ts") or "")
        posted_blocks = 0
        for block in message.thread_blocks:
            self._post_message_payload(
                channel=channel,
                text=block,
                thread_ts=thread_ts,
                operation="post approval thread block",
            )
            posted_blocks += 1
        return enforce_tool_output_guardrails(
            "slack_post_review_message",
            {
                "status": "posted",
                "channel": root.get("channel") or channel or self.approvals_channel or "",
                "ts": thread_ts,
                "thread_blocks_posted": posted_blocks,
                "send_enabled": False,
            },
        )

    def _post_message_payload(
        self,
        *,
        channel: str | None,
        text: str,
        thread_ts: str = "",
        operation: str = "post message",
        blocks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token, default_channel = self._resolved_credentials()
        resolved_channel = channel or default_channel
        if not token or not resolved_channel:
            raise SlackConfigurationError(
                "Live Slack approval notifications require SLACK_BOT_TOKEN and "
                "SLACK_CHANNEL_APPROVALS."
            )

        import requests

        body: dict[str, Any] = {"channel": resolved_channel, "text": text}
        if thread_ts:
            body["thread_ts"] = thread_ts
        if blocks:
            body["blocks"] = blocks
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Slack approval notification failed with HTTP {response.status_code}."
            )
        payload = response.json()
        if not payload.get("ok"):
            error = payload.get("error") or "unknown_error"
            raise RuntimeError(f"Slack approval notification failed: {error}")
        return {"status": "posted", "channel": resolved_channel, "ts": str(payload.get("ts") or "")}
