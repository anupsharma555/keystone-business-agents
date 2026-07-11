"""Safe Slack notification boundary."""

from __future__ import annotations

import json
import os
import re
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
from keystone_agents.slack_action_contract import (
    KBA_APPROVE_EXTERNAL_USE,
    KBA_CREATE_GMAIL_DRAFT,
    KBA_FIND_CONTACT,
    KBA_INTENT_APPROVE_EXTERNAL_USE,
    KBA_INTENT_CREATE_GMAIL_DRAFT,
    KBA_INTENT_FIND_CONTACT,
    KBA_INTENT_MORE_RESEARCH,
    KBA_INTENT_OPEN_WORK_ITEM,
    KBA_INTENT_REVISE_DRAFT,
    KBA_INTENT_RUN_AGAIN,
    KBA_INTENT_SHOW_SOURCES,
    KBA_INTENT_SKIP_COMPANY,
    KBA_MORE_RESEARCH,
    KBA_OVERFLOW,
    KBA_REVISE_DRAFT,
    business_agent_action_value,
)
from keystone_agents.storage.sqlite_store import redact_secrets


class SlackConfigurationError(RuntimeError):
    """Raised when a live Slack call is requested without required credentials."""


SLACK_TEST_MESSAGE_MARKER = "KBA_TEST_SLACK_MESSAGE"
SLACK_TEST_WRITES_ENV = "KEYSTONE_SLACK_ALLOW_TEST_MESSAGE_WRITES"
SLACK_TEST_CHANNEL_ENV = "KEYSTONE_SLACK_TEST_CHANNEL"


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
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b]*(?:\x1b\\|\x07)|[@-Z\\-_])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _looks_like_kni_operator_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    route_terms = (
        "chief of staff",
        "business research",
        "gmail triage",
        "opportunity scout",
        "outreach composer",
        "airtable context",
        "workspace context",
        "zotero context",
        "rss context",
        "preprints context",
    )
    return "@kni" in normalized or (
        bool(re.search(r"<@[a-z0-9]+>", normalized))
        and any(term in normalized for term in route_terms)
    )


@dataclass(frozen=True)
class SlackReviewMessage:
    """Slack-ready review copy with root text and thread-ready detail blocks."""

    root_text: str
    thread_blocks: tuple[str, ...] = field(default_factory=tuple)
    root_blocks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    approval_item_id: str = ""
    object_type: str = ""
    status: str = ""
    scope: str = ""
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    next_safe_action: str = ""
    approval_action_label: str = "review"
    approval_action_detail: str = ""
    send_enabled: bool = False
    interactive_actions_enabled: bool = False

    def as_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for local tests, logs, or future posting."""

        return {
            "root_text": self.root_text,
            "thread_blocks": list(self.thread_blocks),
            "root_blocks": list(self.root_blocks),
            "approval_item_id": self.approval_item_id,
            "object_type": self.object_type,
            "status": self.status,
            "scope": self.scope,
            "risk_flags": list(self.risk_flags),
            "next_safe_action": self.next_safe_action,
            "approval_action_label": self.approval_action_label,
            "approval_action_detail": self.approval_action_detail,
            "send_enabled": self.send_enabled,
            "interactive_actions_enabled": self.interactive_actions_enabled,
        }


@dataclass(frozen=True)
class BusinessAgentCardAction:
    """One compact action rendered into a business-agent Slack card."""

    label: str
    action_id: str
    intent: str
    work_item_id: str = ""
    approval_id: str = ""
    gate_scope: str = ""
    artifact_id: str = ""
    style: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def value(self) -> str:
        return business_agent_action_value(
            intent=self.intent,
            work_item_id=self.work_item_id,
            approval_id=self.approval_id,
            gate_scope=self.gate_scope,
            artifact_id=self.artifact_id,
            metadata=self.metadata,
        )


@dataclass(frozen=True)
class BusinessAgentCard:
    """Reusable compact Slack card model for business-agent WorkItem states."""

    status: str
    summary: str
    fields: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    primary_action: BusinessAgentCardAction | None = None
    secondary_actions: tuple[BusinessAgentCardAction, ...] = field(default_factory=tuple)
    overflow_actions: tuple[BusinessAgentCardAction, ...] = field(default_factory=tuple)
    fallback_text: str = ""


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
    text = str(redacted if redacted is not None else "").replace("\u2014", "-")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_CHAR_RE.sub("", text)
    return text.strip()


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


def _metadata_allows_gmail_draft_creation(
    metadata: dict[str, Any],
    *,
    object_type: str,
) -> bool:
    return (
        object_type == "outreach_draft"
        and _compact_text(metadata.get("outreach_channel"), max_chars=40).lower() == "email"
        and bool(metadata.get("slack_approval_allows_gmail_draft_creation"))
    )


def _approval_action_label(value: Any, *, scope: str) -> str:
    explicit = _public_text(value, max_chars=56)
    if explicit:
        return explicit
    if _enum_value(scope).lower() == "send":
        return "draft review only"
    return _human_label(scope) or "review"


def _approval_action_label_for_item(
    metadata: dict[str, Any],
    *,
    object_type: str,
    scope: str,
) -> str:
    explicit = metadata.get("approval_action_label") or metadata.get("approval_action")
    if explicit:
        return _approval_action_label(explicit, scope=scope)
    if _metadata_allows_gmail_draft_creation(metadata, object_type=object_type):
        return "save Gmail draft"
    return _approval_action_label("", scope=scope)


def _approval_action_detail_for_item(metadata: dict[str, Any], *, object_type: str) -> str:
    if not _metadata_allows_gmail_draft_creation(metadata, object_type=object_type):
        return ""
    account = _public_text(
        metadata.get("gmail_draft_account") or metadata.get("target_gmail_account"),
        max_chars=120,
    )
    if account:
        return f"Gmail draft target: {account}. Approval creates a draft only; no email is sent."
    return (
        "Gmail draft target: configured Gmail account. "
        "Approval creates a draft only; no email is sent."
    )


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


def _button_text(value: str) -> dict[str, str]:
    return {"type": "plain_text", "text": _compact_text(value, max_chars=75)}


_LOCAL_PATH_RE = re.compile(r"(?i)(file://|/Users/|/private/|/tmp/)")
_VISIBLE_SECRET_RE = re.compile(r"(?i)(xox[baprs]-|sk-[a-z0-9]|bearer\s+[a-z0-9._-]{16,})")


def business_agent_card_blocks(card: BusinessAgentCard) -> tuple[dict[str, Any], ...]:
    """Render a compact 4-part business-agent card into Slack Block Kit."""

    status = _public_text(card.status, max_chars=140) or "Ready for review"
    summary = _public_text(card.summary, max_chars=700) or "Review the WorkItem before continuing."
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "block_id": "kba_status",
            "text": {"type": "plain_text", "text": status},
        },
        {
            "type": "section",
            "block_id": "kba_decision_summary",
            "text": {"type": "mrkdwn", "text": summary},
        },
    ]
    fields = [
        {
            "type": "mrkdwn",
            "text": (
                f"*{_public_text(label, max_chars=60)}*\n"
                f"{_public_text(value, max_chars=260) or '-'}"
            ),
        }
        for label, value in card.fields[:10]
        if _clean_slack_text(label)
    ]
    if fields:
        blocks.append({"type": "section", "block_id": "kba_primary_details", "fields": fields})
    elements: list[dict[str, Any]] = []
    if card.primary_action is not None:
        elements.append(_button_element(card.primary_action))
    elements.extend(_button_element(action) for action in card.secondary_actions[:3])
    if card.overflow_actions:
        elements.append(_overflow_element(card.overflow_actions[:10]))
    if elements:
        blocks.append({"type": "actions", "block_id": "kba_actions", "elements": elements})
    validate_business_agent_blocks(blocks)
    return tuple(blocks)


def validate_business_agent_blocks(blocks: Sequence[dict[str, Any]]) -> None:
    """Validate the business-agent card subset we render for Slack."""

    if len(blocks) > 50:
        raise ValueError("Slack business-agent card cannot exceed 50 blocks")
    seen_block_ids: set[str] = set()
    seen_action_ids: set[str] = set()
    visible_text: list[str] = []
    for block in blocks:
        block_id = str(block.get("block_id") or "").strip()
        if block_id:
            if block_id in seen_block_ids:
                raise ValueError(f"duplicate Slack block_id: {block_id}")
            seen_block_ids.add(block_id)
        visible_text.extend(_block_visible_text(block))
        elements = block.get("elements") if isinstance(block.get("elements"), list) else []
        for element in elements:
            if not isinstance(element, dict):
                continue
            action_id = str(element.get("action_id") or "").strip()
            if action_id:
                if action_id in seen_action_ids:
                    raise ValueError(f"duplicate Slack action_id: {action_id}")
                seen_action_ids.add(action_id)
            if element.get("type") == "button":
                label = str((element.get("text") or {}).get("text") or "")
                if len(label) > 75:
                    raise ValueError(f"Slack button label too long: {label}")
            if element.get("type") == "overflow":
                options = element.get("options")
                if not isinstance(options, list) or not options:
                    raise ValueError("Slack overflow must include options")
                for option in options:
                    if not isinstance(option, dict) or not option.get("value"):
                        raise ValueError("Slack overflow options must include values")
                    value = str(option.get("value") or "")
                    if len(value) > 150:
                        raise ValueError("Slack overflow option value cannot exceed 150 chars")
                    label = str((option.get("text") or {}).get("text") or "")
                    if len(label) > 75:
                        raise ValueError(f"Slack overflow label too long: {label}")
    text = "\n".join(visible_text)
    if _LOCAL_PATH_RE.search(text):
        raise ValueError("Slack business-agent card contains a local file path")
    if _VISIBLE_SECRET_RE.search(text):
        raise ValueError("Slack business-agent card contains a visible secret")


def _button_element(action: BusinessAgentCardAction) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "button",
        "text": _button_text(action.label),
        "action_id": action.action_id,
        "value": action.value(),
    }
    if action.style:
        element["style"] = action.style
    return element


def _overflow_element(actions: Sequence[BusinessAgentCardAction]) -> dict[str, Any]:
    return {
        "type": "overflow",
        "action_id": KBA_OVERFLOW,
        "options": [
            {
                "text": _button_text(action.label),
                "value": _overflow_action_value(action),
            }
            for action in actions
        ],
    }


def _overflow_action_value(action: BusinessAgentCardAction) -> str:
    payload = {
        "schema": "kba.v1",
        "intent": action.intent,
        "approval_id": action.approval_id,
    }
    if not action.approval_id and action.work_item_id:
        payload["work_item_id"] = action.work_item_id
    value = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(value) > 150:
        payload.pop("schema", None)
        value = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if len(value) > 150:
        raise ValueError("Slack overflow action value is too long")
    return value


def _block_visible_text(block: dict[str, Any]) -> list[str]:
    text_values: list[str] = []
    for key in ("text", "label"):
        value = block.get(key)
        if isinstance(value, dict):
            text_values.append(str(value.get("text") or ""))
    for block_field in block.get("fields") or []:
        if isinstance(block_field, dict):
            text_values.append(str(block_field.get("text") or ""))
    for element in block.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text = element.get("text")
        if isinstance(text, dict):
            text_values.append(str(text.get("text") or ""))
        for option in element.get("options") or []:
            if isinstance(option, dict) and isinstance(option.get("text"), dict):
                text_values.append(str(option["text"].get("text") or ""))
    return text_values


def _slack_action_blocks(message: SlackReviewMessage) -> list[dict[str, Any]]:
    if not message.approval_item_id or message.approval_item_id == "not saved":
        return []
    action_label = message.approval_action_label or "review"
    return [
        {
            "type": "actions",
            "block_id": "keystone_approval_actions",
            "elements": [
                {
                    "type": "button",
                    "text": _button_text(f"Approve: {action_label}"),
                    "style": "primary",
                    "action_id": "keystone_approval_yes",
                    "value": message.approval_item_id,
                },
                {
                    "type": "button",
                    "text": _button_text(f"Request edits: {action_label}"),
                    "action_id": "keystone_approval_revise",
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
    contact_path_label = _public_text(metadata.get("contact_path_label"), max_chars=140)
    contact_path_value = _public_text(metadata.get("contact_path_value"), max_chars=220)
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
    if contact_path_label or contact_path_value:
        path_text = contact_path_label or "Source-backed contact path"
        if contact_path_value:
            path_text = f"{path_text}: {contact_path_value}"
        rows.append(f"Contact path: {path_text}")
    return rows


def _opportunity_review_rows(metadata: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    opportunity_type = _public_text(metadata.get("opportunity_type"), max_chars=120)
    priority_score = metadata.get("priority_score")
    why_now = _public_text(metadata.get("why_now_signal"), max_chars=260)
    keystone_fit = _public_text(metadata.get("keystone_fit_reason"), max_chars=260)
    next_step = _public_text(metadata.get("opportunity_next_step"), max_chars=220)
    if opportunity_type:
        rows.append(f"Opportunity type: {opportunity_type}")
    if priority_score is not None:
        rows.append(f"Priority score: {priority_score}/100")
    if why_now:
        rows.append(f"Why now: {why_now}")
    if keystone_fit:
        rows.append(f"Keystone fit: {keystone_fit}")
    if next_step:
        rows.append(f"Next step: {next_step}")
    return rows


def _company_research_rows(metadata: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    company = _public_text(metadata.get("company_name"), max_chars=140)
    website = _public_text(metadata.get("company_website"), max_chars=220)
    summary = _public_text(
        metadata.get("company_fit_summary") or metadata.get("company_description"),
        max_chars=320,
    )
    if summary:
        rows.append(f"CR summary: {summary}")
    metadata_rows: list[str] = []
    if company:
        metadata_rows.append(f"Company: {company}")
    if website:
        metadata_rows.append(f"Company link: {website}")
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
        metadata_rows.append(f"CR scores: {score_text}")
    source_quality = _as_dict(metadata.get("company_source_quality"))
    if source_quality.get("overall_score") is not None:
        metadata_rows.append(
            "Source quality: "
            f"{source_quality.get('overall_score')}/100; "
            f"independent sources {source_quality.get('independent_source_count', 0)}; "
            f"high-quality sources {source_quality.get('high_quality_source_count', 0)}"
        )
    completeness = _as_dict(metadata.get("company_research_completeness"))
    if completeness.get("score") is not None:
        metadata_rows.append(f"Research completeness: {completeness.get('score')}/100")
    gaps = [
        _public_text(gap, max_chars=180)
        for gap in (metadata.get("company_missing_information") or [])
    ]
    gaps = [gap for gap in gaps if gap]
    if gaps:
        metadata_rows.append("CR gaps:")
        metadata_rows.extend(f"- {gap}" for gap in gaps[:2])
    risks = [_public_text(risk, max_chars=180) for risk in (metadata.get("company_risks") or [])]
    risks = [risk for risk in risks if risk]
    if risks:
        metadata_rows.append("CR risks:")
        metadata_rows.extend(f"- {risk}" for risk in risks[:2])
    source_rows = _source_rows(_sources_from_metadata(metadata))
    if source_rows:
        metadata_rows.append("Top sources:")
        metadata_rows.extend(source_rows[:3])
    if metadata_rows:
        rows.append("Metadata:")
        rows.extend(metadata_rows)
    return rows


def _draft_review_rows(
    metadata: dict[str, Any],
    draft_text: Any,
    *,
    max_thread_chars: int,
) -> list[str]:
    rows: list[str] = [
        "Draft text:",
        _thread_text(draft_text, max_chars=max_thread_chars) or "- none",
        "Metadata:",
    ]
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
        rows.extend(f"- {blocker}" for blocker in blockers[:3])
    rows.append("Draft only. Human approval is required before any external use.")
    return rows


def _business_agent_card_for_approval_item(
    data: dict[str, Any],
    metadata: dict[str, Any],
    *,
    status: str,
    object_type: str,
    approval_scope: str,
    summary: str,
) -> BusinessAgentCard:
    approval_id = _enum_value(data.get("id"))
    work_item_id = _compact_text(metadata.get("work_item_id"), max_chars=120)
    artifact_id = _enum_value(data.get("object_id")) or _compact_text(
        metadata.get("artifact_id"), max_chars=120
    )
    gmail_draft_allowed = _metadata_allows_gmail_draft_creation(
        metadata,
        object_type=object_type,
    )
    status_label = _approval_card_status(
        status,
        metadata=metadata,
        gmail_draft_allowed=gmail_draft_allowed,
    )
    fields = tuple(_approval_card_fields(metadata, data, approval_scope=approval_scope))
    action_context = {
        "approval_title": _public_text(data.get("title"), max_chars=120),
        "object_type": object_type,
        "gmail_draft_account": _public_text(
            metadata.get("gmail_draft_account") or metadata.get("target_gmail_account"),
            max_chars=120,
        ),
    }
    primary_action: BusinessAgentCardAction | None = None
    secondary_actions: tuple[BusinessAgentCardAction, ...] = ()
    overflow_actions: tuple[BusinessAgentCardAction, ...] = ()
    if status == "pending" and approval_id:
        if gmail_draft_allowed:
            primary_action = BusinessAgentCardAction(
                label="Create Gmail draft",
                action_id=KBA_CREATE_GMAIL_DRAFT,
                intent=KBA_INTENT_CREATE_GMAIL_DRAFT,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                style="primary",
                metadata=action_context,
            )
        else:
            scope_label = (
                "draft review"
                if approval_scope == "send"
                else (_human_label(approval_scope) or "external use")
            )
            primary_action = BusinessAgentCardAction(
                label=f"Approve {scope_label}",
                action_id=KBA_APPROVE_EXTERNAL_USE,
                intent=KBA_INTENT_APPROVE_EXTERNAL_USE,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                style="primary",
                metadata=action_context,
            )
        secondary_actions = (
            BusinessAgentCardAction(
                label="Revise draft",
                action_id=KBA_REVISE_DRAFT,
                intent=KBA_INTENT_REVISE_DRAFT,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                metadata=action_context,
            ),
            BusinessAgentCardAction(
                label="Retry source pass",
                action_id=KBA_MORE_RESEARCH,
                intent=KBA_INTENT_MORE_RESEARCH,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                metadata=action_context,
            ),
            BusinessAgentCardAction(
                label="Find contact",
                action_id=KBA_FIND_CONTACT,
                intent=KBA_INTENT_FIND_CONTACT,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                metadata=action_context,
            ),
        )
        overflow = [
            BusinessAgentCardAction(
                label="Show sources",
                action_id=KBA_OVERFLOW,
                intent=KBA_INTENT_SHOW_SOURCES,
                work_item_id=work_item_id,
                approval_id=approval_id,
                gate_scope=approval_scope,
                artifact_id=artifact_id,
                metadata=action_context,
            )
        ]
        if work_item_id:
            overflow.append(
                BusinessAgentCardAction(
                    label="Open WorkItem",
                    action_id=KBA_OVERFLOW,
                    intent=KBA_INTENT_OPEN_WORK_ITEM,
                    work_item_id=work_item_id,
                    approval_id=approval_id,
                    gate_scope=approval_scope,
                    artifact_id=artifact_id,
                    metadata=action_context,
                )
            )
        overflow.extend(
            [
                BusinessAgentCardAction(
                    label="Run again",
                    action_id=KBA_OVERFLOW,
                    intent=KBA_INTENT_RUN_AGAIN,
                    work_item_id=work_item_id,
                    approval_id=approval_id,
                    gate_scope=approval_scope,
                    artifact_id=artifact_id,
                    metadata=action_context,
                ),
                BusinessAgentCardAction(
                    label="Skip company",
                    action_id=KBA_OVERFLOW,
                    intent=KBA_INTENT_SKIP_COMPANY,
                    work_item_id=work_item_id,
                    approval_id=approval_id,
                    gate_scope=approval_scope,
                    artifact_id=artifact_id,
                    metadata=action_context,
                ),
            ]
        )
        overflow_actions = tuple(overflow)
    return BusinessAgentCard(
        status=status_label,
        summary=summary,
        fields=fields,
        primary_action=primary_action,
        secondary_actions=secondary_actions,
        overflow_actions=overflow_actions,
        fallback_text=_approval_card_fallback_text(
            status_label,
            summary,
            fields,
            gmail_draft_allowed=gmail_draft_allowed,
            metadata=metadata,
        ),
    )


def _approval_card_status(
    status: str,
    *,
    metadata: dict[str, Any],
    gmail_draft_allowed: bool,
) -> str:
    if status == "approved":
        if metadata.get("gmail_draft_created_from_slack_approval"):
            return "Draft created"
        return "Approved"
    if status == "revise":
        return "Revision requested"
    if status == "rejected":
        return "Rejected"
    if _card_contact_missing(metadata):
        return "Needs contact"
    if gmail_draft_allowed:
        return "Draft ready"
    return "Ready for approval"


def _approval_card_fields(
    metadata: dict[str, Any],
    data: dict[str, Any],
    *,
    approval_scope: str,
) -> list[tuple[str, str]]:
    channel = _compact_text(metadata.get("outreach_channel"), max_chars=40).lower()
    destination = _card_destination(metadata)
    fields: list[tuple[str, str]] = []
    company = _public_text(metadata.get("company_name"), max_chars=120)
    if company:
        fields.append(("Company", company))
    contact = _card_contact(metadata)
    if contact:
        fields.append(("Contact", contact))
    if destination:
        label = "Destination" if channel in {"email", "linkedin"} else "Target"
        fields.append((label, destination))
    priority_score = metadata.get("priority_score")
    if priority_score is not None:
        fields.append(("Fit score", f"{priority_score}/100"))
    source_quality = _card_source_quality(metadata)
    if source_quality:
        fields.append(("Evidence", source_quality))
    missing = _card_missing_info(metadata)
    if missing:
        fields.append(("Missing info", missing))
    fields.append(("Gate", _human_label(approval_scope) or approval_scope))
    if data.get("id"):
        fields.append(("Approval ID", _public_text(data.get("id"), max_chars=120)))
    return fields[:10]


def _card_contact(metadata: dict[str, Any]) -> str:
    name = _public_text(metadata.get("contact_name"), max_chars=120)
    title = _public_text(metadata.get("contact_title"), max_chars=120)
    if name and title:
        return f"{name}, {title}"
    return name or title


def _card_destination(metadata: dict[str, Any]) -> str:
    channel = _compact_text(metadata.get("outreach_channel"), max_chars=40).lower()
    email = _public_text(metadata.get("recipient_email"), max_chars=160)
    linkedin_url = _public_text(
        metadata.get("contact_linkedin_url") or metadata.get("linkedin_url"),
        max_chars=180,
    )
    if channel == "linkedin":
        return linkedin_url or "LinkedIn URL needed"
    if channel == "email":
        return email or "Email needed"
    return email or linkedin_url


def _card_contact_missing(metadata: dict[str, Any]) -> bool:
    channel = _compact_text(metadata.get("outreach_channel"), max_chars=40).lower()
    if channel == "email":
        return not bool(_public_text(metadata.get("recipient_email"), max_chars=160))
    if channel == "linkedin":
        return not bool(
            _public_text(
                metadata.get("contact_linkedin_url") or metadata.get("linkedin_url"),
                max_chars=180,
            )
        )
    return False


def _card_source_quality(metadata: dict[str, Any]) -> str:
    source_quality = _as_dict(metadata.get("company_source_quality"))
    if source_quality.get("overall_score") is not None:
        count = source_quality.get("independent_source_count", 0)
        return f"{source_quality.get('overall_score')}/100; {count} independent source(s)"
    sources = _sources_from_metadata(metadata)
    if sources:
        return f"{len(sources)} source(s)"
    return "Needs source check"


def _card_missing_info(metadata: dict[str, Any]) -> str:
    values = (
        metadata.get("missing_information_blockers")
        or metadata.get("company_missing_information")
        or []
    )
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        values = [values]
    cleaned = [_public_text(value, max_chars=140) for value in values]
    cleaned = [value for value in cleaned if value]
    return cleaned[0] if cleaned else "None"


def _approval_card_fallback_text(
    status: str,
    summary: str,
    fields: Sequence[tuple[str, str]],
    *,
    gmail_draft_allowed: bool,
    metadata: dict[str, Any],
) -> str:
    rows = [status, summary]
    rows.extend(f"{label}: {value}" for label, value in fields if value)
    if gmail_draft_allowed:
        account = _public_text(
            metadata.get("gmail_draft_account") or metadata.get("target_gmail_account"),
            max_chars=120,
        )
        rows.append(
            "Primary action: Create Gmail draft"
            f"{f' in {account}' if account else ''}. No email is sent."
        )
    rows.append(
        "Slack buttons update only the referenced WorkItem gate or queue a draft-only agent step."
    )
    return "\n".join(row for row in rows if _clean_slack_text(row))


def format_slack_review_message(
    card: ReviewCard | Mapping[str, Any],
    *,
    approval_item_id: str = "",
    object_type: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    risk_flags: Sequence[str] | None = None,
    next_safe_action: str | None = None,
    approval_action_label: str | None = None,
    approval_action_detail: str | None = None,
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
    resolved_action_label = _approval_action_label(approval_action_label, scope=resolved_scope)
    resolved_action_detail = _public_text(approval_action_detail, max_chars=260)
    display_title = _public_text(data.title, max_chars=140) or _human_label(resolved_type)

    lowered_title = display_title.lower()
    if "linkedin" in lowered_title:
        channel = "LinkedIn"
    elif "email" in lowered_title:
        channel = "email"
    else:
        channel = _human_label(resolved_type)
    review_target = channel if channel.lower().endswith("draft") else f"{channel} draft"
    root_sections = [
        "Ready for approval",
        f"Review: {display_title}",
        f"Approval ID: {resolved_id}",
        (
            f"Type: {_human_label(resolved_type)} | Channel: {channel} | "
            f"Status: {_human_label(resolved_status)} | Scope: {_human_label(resolved_scope)} | "
            f"Risk flags: {_join_human_labels(resolved_risks)}"
        ),
        f"Decision: {_public_text(data.decision_summary, max_chars=220)}",
        (
            f"Approval question: approve the action `{resolved_action_label}` "
            f"for this {review_target}?"
        ),
        f"Button scope: Approve: {resolved_action_label}; Request edits: {resolved_action_label}.",
        resolved_action_detail,
        f"Next safe action: {resolved_next}",
        (
            "Slack buttons update only the named approval scope. They do not send email, "
            "post externally, publish, or schedule anything. Gmail draft creation happens "
            "only when this review explicitly says save Gmail draft."
        ),
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
        approval_action_label=resolved_action_label,
        approval_action_detail=resolved_action_detail,
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
    approval_scope = _scope_for_queue_item(data)
    action_label = _approval_action_label_for_item(
        metadata,
        object_type=object_type,
        scope=approval_scope,
    )
    action_detail = _approval_action_detail_for_item(metadata, object_type=object_type)
    decision_summary = _public_text(data.get("summary"), max_chars=240) or (
        "Approval review requested."
    )
    card = ReviewCard(
        title=_public_text(data.get("title"), max_chars=160) or "Approval review",
        object_type=_review_object_type(object_type),
        object_id=_enum_value(data.get("object_id")) or _enum_value(data.get("id")),
        decision_summary=decision_summary,
        reason=_public_text(data.get("summary"), max_chars=420)
        or "Human review is required before external use.",
        evidence=_evidence_from_metadata(metadata),
        risks=data.get("risk_flags") or [],
        approval_required=True,
        approval_status=status,
        approval_scope=approval_scope,
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
        approval_action_label=action_label,
        approval_action_detail=action_detail,
        draft_text=data.get("draft_text"),
        max_thread_chars=max_thread_chars,
    )
    root_sections = message.root_text.split("\n\n")
    review_rows = [*_contact_review_rows(metadata), *_opportunity_review_rows(metadata)]
    root_sections.insert(3, "\n".join(review_rows))
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
    business_card = _business_agent_card_for_approval_item(
        data,
        metadata,
        status=status,
        object_type=object_type,
        approval_scope=approval_scope,
        summary=decision_summary,
    )
    root_blocks = business_agent_card_blocks(business_card)
    return SlackReviewMessage(
        root_text=business_card.fallback_text
        or "\n\n".join(section for section in root_sections if section.strip()),
        thread_blocks=thread_blocks,
        root_blocks=root_blocks,
        approval_item_id=message.approval_item_id,
        object_type=message.object_type,
        status=message.status,
        scope=message.scope,
        risk_flags=tuple(message.risk_flags),
        next_safe_action=message.next_safe_action,
        approval_action_label=message.approval_action_label,
        approval_action_detail=message.approval_action_detail,
        send_enabled=message.send_enabled,
        interactive_actions_enabled=message.interactive_actions_enabled,
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

        root_blocks = (
            list(message.root_blocks)
            if message.root_blocks
            else [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": message.root_text},
                },
                *_slack_action_blocks(message),
            ]
        )
        root = self._post_message_payload(
            channel=channel,
            text=message.root_text,
            blocks=root_blocks,
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

    def read_message(self, channel: str, ts: str) -> dict[str, Any]:
        """Read one exact Slack message for provider verification."""

        clean_channel = channel.strip()
        clean_ts = ts.strip()
        if not clean_channel or not clean_ts:
            raise ValueError("Slack message reads require exact channel and timestamp.")
        if not self.live:
            return {"status": "dry-run", "channel": clean_channel, "ts": clean_ts}
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack message reads require SLACK_BOT_TOKEN.")
        payload = _slack_api_request(
            "GET",
            "conversations.history",
            token=token,
            params={
                "channel": clean_channel,
                "oldest": clean_ts,
                "latest": clean_ts,
                "inclusive": "true",
                "limit": 1,
            },
        )
        messages = payload.get("messages", [])
        match = next(
            (
                item
                for item in messages
                if isinstance(item, Mapping) and str(item.get("ts") or "") == clean_ts
            ),
            None,
        ) if isinstance(messages, list) else None
        if not isinstance(match, Mapping):
            return {"status": "not_found", "channel": clean_channel, "ts": clean_ts}
        return {
            "status": "success",
            "channel": clean_channel,
            "ts": clean_ts,
            "text": str(match.get("text") or ""),
            "bot_id": str(match.get("bot_id") or ""),
        }

    def list_recent_thread_roots(self, channel: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """List bounded recent thread roots without returning message text."""

        clean_channel = channel.strip()
        if not clean_channel:
            raise ValueError("Slack thread-root reads require an exact channel.")
        if not self.live:
            return []
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack thread reads require SLACK_BOT_TOKEN.")
        requested = max(1, min(200, int(limit)))
        scanned = 0
        cursor = ""
        roots: list[dict[str, Any]] = []
        while scanned < requested:
            params: dict[str, Any] = {
                "channel": clean_channel,
                "limit": min(100, requested - scanned),
            }
            if cursor:
                params["cursor"] = cursor
            payload = _slack_api_request(
                "GET",
                "conversations.history",
                token=token,
                params=params,
            )
            messages = payload.get("messages", [])
            if not isinstance(messages, list) or not messages:
                break
            scanned += len(messages)
            roots.extend(
                {
                    "channel": clean_channel,
                    "thread_ts": str(item.get("ts") or ""),
                    "reply_count": int(item.get("reply_count") or 0),
                }
                for item in messages
                if isinstance(item, Mapping)
                and str(item.get("ts") or "")
                and not str(item.get("thread_ts") or "")
            )
            metadata = payload.get("response_metadata")
            cursor = (
                str(metadata.get("next_cursor") or "").strip()
                if isinstance(metadata, Mapping)
                else ""
            )
            if not cursor:
                break
        return roots

    def read_thread(self, channel: str, thread_ts: str, *, limit: int = 50) -> dict[str, Any]:
        """Read one exact bounded Slack thread without posting or modifying it."""

        clean_channel = channel.strip()
        clean_ts = thread_ts.strip()
        if not clean_channel or not clean_ts:
            raise ValueError("Slack thread reads require exact channel and root timestamp.")
        if not self.live:
            return {
                "status": "dry-run",
                "channel": clean_channel,
                "thread_ts": clean_ts,
                "messages": [],
                "post_enabled": False,
            }
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack thread reads require SLACK_BOT_TOKEN.")
        payload = _slack_api_request(
            "GET",
            "conversations.replies",
            token=token,
            params={
                "channel": clean_channel,
                "ts": clean_ts,
                "limit": max(1, min(100, int(limit))),
            },
        )
        messages = payload.get("messages", [])
        bounded = [
            {
                "ts": str(item.get("ts") or ""),
                "text": str(item.get("text") or ""),
                "user": str(item.get("user") or ""),
                "bot_id": str(item.get("bot_id") or ""),
            }
            for item in messages
            if isinstance(item, Mapping) and str(item.get("ts") or "")
        ] if isinstance(messages, list) else []
        return {
            "status": "success" if bounded else "not_found",
            "channel": clean_channel,
            "thread_ts": clean_ts,
            "message_count": len(bounded),
            "messages": bounded,
            "post_enabled": False,
        }

    def resolve_latest_thread_root(
        self,
        channel: str,
        *,
        scan_limit: int = 20,
    ) -> dict[str, Any]:
        """Resolve the newest actual reply thread when history omits reply counts."""

        clean_channel = channel.strip()
        if not clean_channel:
            raise ValueError("Latest Slack thread resolution requires an exact channel.")
        roots = self.list_recent_thread_roots(
            clean_channel,
            limit=max(1, min(50, int(scan_limit))),
        )
        direct = next((root for root in roots if int(root.get("reply_count") or 0) > 0), None)
        if direct is not None or not self.live:
            return direct or {}
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack thread reads require SLACK_BOT_TOKEN.")
        for root in roots:
            thread_ts = str(root.get("thread_ts") or "").strip()
            if not thread_ts:
                continue
            payload = _slack_api_request(
                "GET",
                "conversations.replies",
                token=token,
                params={"channel": clean_channel, "ts": thread_ts, "limit": 2},
            )
            messages = payload.get("messages", [])
            if isinstance(messages, list) and len(messages) > 1:
                return {
                    "channel": clean_channel,
                    "thread_ts": thread_ts,
                    "reply_count": len(messages) - 1,
                    "resolution": "bounded_replies_probe",
                }
        return {}

    def find_latest_kni_request(
        self,
        channel: str,
        *,
        scan_limit: int = 200,
    ) -> dict[str, Any]:
        """Return the newest bounded user-authored KNI request and no unrelated text."""

        clean_channel = channel.strip()
        if not clean_channel:
            raise ValueError("Latest KNI request lookup requires an exact channel.")
        if not self.live:
            return {}
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack request lookup requires SLACK_BOT_TOKEN.")
        requested = max(1, min(200, int(scan_limit)))
        scanned = 0
        cursor = ""
        while scanned < requested:
            params: dict[str, Any] = {
                "channel": clean_channel,
                "limit": min(100, requested - scanned),
            }
            if cursor:
                params["cursor"] = cursor
            payload = _slack_api_request(
                "GET",
                "conversations.history",
                token=token,
                params=params,
            )
            messages = payload.get("messages", [])
            if not isinstance(messages, list) or not messages:
                break
            scanned += len(messages)
            for item in messages:
                if not isinstance(item, Mapping):
                    continue
                text = str(item.get("text") or "").strip()
                if (
                    text
                    and str(item.get("user") or "").strip()
                    and not str(item.get("bot_id") or "").strip()
                    and _looks_like_kni_operator_request(text)
                ):
                    return {
                        "channel": clean_channel,
                        "ts": str(item.get("ts") or ""),
                        "text": text,
                        "user_present": True,
                    }
            metadata = payload.get("response_metadata")
            cursor = (
                str(metadata.get("next_cursor") or "").strip()
                if isinstance(metadata, Mapping)
                else ""
            )
            if not cursor:
                break
        return {}

    def find_test_messages(self, channel: str, exact_text: str, *, limit: int = 20) -> list[str]:
        """Return timestamps for recent exact marked messages without exposing other text."""

        clean_channel = channel.strip()
        if not clean_channel or SLACK_TEST_MESSAGE_MARKER not in exact_text:
            raise ValueError("Slack test-message recovery requires exact channel and marked text.")
        if not self.live:
            return []
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack message recovery requires SLACK_BOT_TOKEN.")
        payload = _slack_api_request(
            "GET",
            "conversations.history",
            token=token,
            params={"channel": clean_channel, "limit": max(1, min(50, int(limit)))},
        )
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return []
        return [
            str(item.get("ts") or "")
            for item in messages
            if isinstance(item, Mapping)
            and str(item.get("text") or "") == exact_text
            and str(item.get("ts") or "")
        ]

    def post_test_message(
        self,
        channel: str,
        text: str,
        *,
        approval_reference: str,
    ) -> dict[str, Any]:
        """Post and verify one exact marked Slack test message."""

        clean_channel = _require_slack_test_write_scope(
            channel,
            text=text,
            approval_reference=approval_reference,
            live=self.live,
        )
        if not self.live:
            return {
                "status": "dry-run",
                "operation": "post_test_message",
                "channel": clean_channel,
                "ts": "",
                "verification": {"status": "preview", "passed": False},
            }
        posted = self._post_message_payload(channel=clean_channel, text=text)
        ts = str(posted.get("ts") or "")
        after = self.read_message(clean_channel, ts)
        passed = after.get("status") == "success" and after.get("text") == text
        return {
            **posted,
            "status": "posted" if passed else "verification_failed",
            "operation": "post_test_message",
            "approval_reference": approval_reference.strip(),
            "verification": {
                "status": "verified" if passed else "verification_failed",
                "passed": passed,
                "channel_match": after.get("channel") == clean_channel,
                "ts_match": after.get("ts") == ts,
                "text_match": after.get("text") == text,
            },
        }

    def update_test_message(
        self,
        channel: str,
        ts: str,
        text: str,
        *,
        approval_reference: str,
    ) -> dict[str, Any]:
        """Update and verify one exact bot-authored marked Slack test message."""

        clean_channel = _require_slack_test_write_scope(
            channel,
            text=text,
            approval_reference=approval_reference,
            live=self.live,
        )
        clean_ts = ts.strip()
        if not clean_ts:
            raise ValueError("Slack test-message update requires exact timestamp.")
        if not self.live:
            return {
                "status": "dry-run",
                "operation": "update_test_message",
                "channel": clean_channel,
                "ts": clean_ts,
                "verification": {"status": "preview", "passed": False},
            }
        before = self.read_message(clean_channel, clean_ts)
        if SLACK_TEST_MESSAGE_MARKER not in str(before.get("text") or ""):
            raise RuntimeError("Slack test update refused: provider message lacks test marker.")
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack message updates require SLACK_BOT_TOKEN.")
        _slack_api_request(
            "POST",
            "chat.update",
            token=token,
            json_body={"channel": clean_channel, "ts": clean_ts, "text": text},
        )
        after = self.read_message(clean_channel, clean_ts)
        passed = after.get("status") == "success" and after.get("text") == text
        return {
            "status": "updated" if passed else "verification_failed",
            "operation": "update_test_message",
            "channel": clean_channel,
            "ts": clean_ts,
            "approval_reference": approval_reference.strip(),
            "verification": {
                "status": "verified" if passed else "verification_failed",
                "passed": passed,
                "same_ts": after.get("ts") == clean_ts,
                "text_match": after.get("text") == text,
            },
        }

    def delete_test_message(
        self,
        channel: str,
        ts: str,
        *,
        approval_reference: str,
    ) -> dict[str, Any]:
        """Delete one exact marked Slack test message and verify absence."""

        clean_channel = _require_slack_test_write_scope(
            channel,
            text=SLACK_TEST_MESSAGE_MARKER,
            approval_reference=approval_reference,
            live=self.live,
        )
        clean_ts = ts.strip()
        if not clean_ts:
            raise ValueError("Slack test-message deletion requires exact timestamp.")
        if not self.live:
            return {
                "status": "dry-run",
                "operation": "delete_test_message",
                "channel": clean_channel,
                "ts": clean_ts,
                "verification": {"status": "preview", "passed": False},
            }
        before = self.read_message(clean_channel, clean_ts)
        if SLACK_TEST_MESSAGE_MARKER not in str(before.get("text") or ""):
            raise RuntimeError("Slack test deletion refused: provider message lacks test marker.")
        token, _default_channel = self._resolved_credentials()
        if not token:
            raise SlackConfigurationError("Live Slack message deletion requires SLACK_BOT_TOKEN.")
        _slack_api_request(
            "POST",
            "chat.delete",
            token=token,
            json_body={"channel": clean_channel, "ts": clean_ts},
        )
        absent = self.read_message(clean_channel, clean_ts).get("status") == "not_found"
        return {
            "status": "deleted" if absent else "verification_failed",
            "operation": "delete_test_message",
            "channel": clean_channel,
            "ts": clean_ts,
            "approval_reference": approval_reference.strip(),
            "verification": {
                "status": "verified" if absent else "verification_failed",
                "passed": absent,
                "message_absent_after": absent,
                "test_marker_verified": True,
            },
        }

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


def _require_slack_test_write_scope(
    channel: str,
    *,
    text: str,
    approval_reference: str,
    live: bool,
) -> str:
    clean_channel = channel.strip()
    if not clean_channel or not approval_reference.strip():
        raise RuntimeError("Slack test writes require exact channel and approval reference.")
    if SLACK_TEST_MESSAGE_MARKER not in text:
        raise RuntimeError("Slack test writes require the exact test marker in message text.")
    if live:
        allowed = os.getenv(SLACK_TEST_CHANNEL_ENV, "").strip()
        enabled = os.getenv(SLACK_TEST_WRITES_ENV, "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not enabled:
            raise RuntimeError(f"Slack test writes require {SLACK_TEST_WRITES_ENV}=true.")
        if not allowed or allowed != clean_channel:
            raise RuntimeError("Slack test write channel is not the exact configured channel.")
    return clean_channel


def _slack_api_request(
    method: str,
    endpoint: str,
    *,
    token: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import requests

    response = requests.request(
        method,
        f"https://slack.com/api/{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        params=params,
        json=json_body,
        timeout=10,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Slack {endpoint} failed with HTTP {response.status_code}.")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        error = payload.get("error") if isinstance(payload, dict) else "invalid_response"
        raise RuntimeError(f"Slack {endpoint} failed: {error or 'unknown_error'}")
    return payload
