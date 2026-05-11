"""Slack interactive approval handling for local Keystone approval items."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from pydantic import BaseModel

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.schemas.approval import ApprovalQueueObjectType, ApprovalQueueStatus
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.gmail_tool import GmailTool

ACTION_STATUS_BY_ID = {
    "keystone_approval_yes": ApprovalQueueStatus.APPROVED,
    "keystone_approval_no": ApprovalQueueStatus.REJECTED,
    "keystone_approval_revise": ApprovalQueueStatus.REVISE,
}


class SlackApprovalInteractionResult(BaseModel):
    """Result of applying one Slack approval action locally."""

    approval_id: str
    action_id: str
    approval_status: ApprovalQueueStatus
    reviewer: str = ""
    notes: str = ""
    outcome: str = ""
    followup_text: str = ""
    gmail_draft_result: dict[str, Any] | None = None
    revision_prompt: str = ""
    feedback_prompt: str = ""
    source: str = "slack_interaction"
    send_enabled: bool = False
    email_sent: bool = False


class SlackAgentMentionRoute(BaseModel):
    """Parsed Slack app mention routed to a Keystone agent."""

    route: str
    agent_name: str
    input_text: str
    explicit: bool = False
    send_enabled: bool = False


def parse_slack_interaction_payload(raw_payload: str | dict[str, Any]) -> dict[str, Any]:
    """Parse a Slack interactive payload from JSON or URL-encoded form content."""

    if isinstance(raw_payload, dict):
        return raw_payload
    text = raw_payload.strip()
    if not text:
        raise ValueError("Slack interaction payload cannot be empty.")
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Slack interaction JSON payload must be an object.")
        return data
    parsed = parse_qs(text)
    payload_values = parsed.get("payload")
    if not payload_values:
        raise ValueError("URL-encoded Slack interaction payload must include payload=...")
    data = json.loads(payload_values[0])
    if not isinstance(data, dict):
        raise ValueError("Slack interaction payload must decode to an object.")
    return data


def parse_slack_agent_mention(text: str) -> SlackAgentMentionRoute:
    """Resolve a Slack app mention such as `<@BOT> business research analyst ...`."""

    mention = parse_agent_mention(text)
    route = mention.route or "orchestrator"
    return SlackAgentMentionRoute(
        route=route,
        agent_name=mention.agent_name,
        input_text=mention.input_text,
        explicit=mention.explicit,
    )


def handle_slack_approval_interaction(
    raw_payload: str | dict[str, Any],
    *,
    database_url: str | None = None,
    create_email_draft: bool = True,
    live_gmail: bool = False,
) -> SlackApprovalInteractionResult:
    """Apply a Slack Yes/No/Needs edits action to the local approval queue."""

    payload = parse_slack_interaction_payload(raw_payload)
    action = _first_action(payload)
    action_id = str(action.get("action_id") or "").strip()
    status = ACTION_STATUS_BY_ID.get(action_id)
    if status is None:
        raise ValueError(f"Unsupported Slack approval action: {action_id or 'unknown'}")

    approval_id = str(action.get("value") or "").strip()
    if not approval_id:
        raise ValueError("Slack approval action is missing an approval item id.")

    reviewer = _reviewer(payload)
    feedback = _feedback_text(payload)
    notes = feedback or _default_notes(status)
    store = SQLiteStore(database_url or database_url_from_env())
    item = store.update_approval_status(
        approval_id,
        status,
        reviewer=reviewer,
        notes=notes,
    )
    metadata = dict(item.metadata or {})
    gmail_draft_result: dict[str, Any] | None = None
    revision_prompt = ""
    feedback_prompt = ""
    followup_text = _followup_text_for_status(status, item, feedback=feedback)

    if status == ApprovalQueueStatus.APPROVED and create_email_draft:
        gmail_draft_result = _maybe_create_email_draft(item, live_gmail=live_gmail)
        if gmail_draft_result is not None:
            metadata["gmail_draft_result"] = gmail_draft_result
            metadata["gmail_draft_created_from_slack_approval"] = bool(
                gmail_draft_result.get("status") == "draft_created"
            )
            item = _save_metadata(store, item, metadata)
            followup_text = (
                "Approved. Gmail draft was created for human review; no email was sent."
                if gmail_draft_result.get("status") == "draft_created"
                else "Approved. Gmail draft creation returned a dry-run preview; no email was sent."
            )
    elif status == ApprovalQueueStatus.REVISE:
        revision_prompt = _revision_prompt(item, feedback=feedback)
        metadata["llm_revision_prompt"] = revision_prompt
        metadata["revision_feedback_required"] = not bool(feedback)
        item = _save_metadata(store, item, metadata)
    elif status == ApprovalQueueStatus.REJECTED:
        feedback_prompt = _rejection_feedback_prompt(item)
        metadata["rejection_feedback_prompt"] = feedback_prompt
        metadata["rejection_feedback_required"] = not bool(feedback)
        item = _save_metadata(store, item, metadata)

    return SlackApprovalInteractionResult(
        approval_id=item.id,
        action_id=action_id,
        approval_status=item.approval_status,
        reviewer=item.reviewer or reviewer,
        notes=item.reviewer_notes or notes,
        outcome=_outcome_label(status, item),
        followup_text=followup_text,
        gmail_draft_result=gmail_draft_result,
        revision_prompt=revision_prompt,
        feedback_prompt=feedback_prompt,
    )


def _first_action(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Slack interaction payload has no actions.")
    action = actions[0]
    if not isinstance(action, dict):
        raise ValueError("Slack interaction action must be an object.")
    return action


def _reviewer(payload: dict[str, Any]) -> str:
    user = payload.get("user")
    if not isinstance(user, dict):
        return "slack"
    return str(user.get("username") or user.get("name") or user.get("id") or "slack")


def _feedback_text(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    values = state.get("values", {}) if isinstance(state, dict) else {}
    if not isinstance(values, dict):
        return ""
    for block in values.values():
        if not isinstance(block, dict):
            continue
        for item in block.values():
            if isinstance(item, dict) and item.get("type") == "plain_text_input":
                text = str(item.get("value") or "").strip()
                if text:
                    return text
    return ""


def _default_notes(status: ApprovalQueueStatus) -> str:
    if status == ApprovalQueueStatus.APPROVED:
        return "Approved from Slack interactive review."
    if status == ApprovalQueueStatus.REJECTED:
        return "Rejected from Slack interactive review."
    if status == ApprovalQueueStatus.REVISE:
        return "Needs edits from Slack interactive review."
    return "Updated from Slack interactive review."


def _save_metadata(
    store: SQLiteStore,
    item: Any,
    metadata: dict[str, Any],
) -> Any:
    store.save_approval_item(item.model_copy(update={"metadata": metadata}))
    return store.get_approval_item(item.id) or item


def _outcome_label(status: ApprovalQueueStatus, item: Any) -> str:
    if status == ApprovalQueueStatus.APPROVED and _is_email_outreach_item(item):
        return "approved_email_draft"
    if status == ApprovalQueueStatus.APPROVED:
        return "approved"
    if status == ApprovalQueueStatus.REVISE:
        return "revision_requested"
    if status == ApprovalQueueStatus.REJECTED:
        return "rejected_feedback_requested"
    return status.value


def _followup_text_for_status(
    status: ApprovalQueueStatus,
    item: Any,
    *,
    feedback: str,
) -> str:
    if status == ApprovalQueueStatus.APPROVED and _is_email_outreach_item(item):
        return "Approved. A Gmail draft can now be created; no email will be sent."
    if status == ApprovalQueueStatus.APPROVED:
        return "Approved for the recorded scope. No external message was sent."
    if status == ApprovalQueueStatus.REVISE:
        if feedback:
            return "Needs edits captured. The revision prompt is ready for the LLM."
        return "Needs edits captured. Reply with edit instructions for the LLM before re-drafting."
    if status == ApprovalQueueStatus.REJECTED:
        if feedback:
            return "Rejected with feedback captured."
        return "Rejected. Please reply with a short reason so the agent can learn."
    return "Approval item updated."


def _is_email_outreach_item(item: Any) -> bool:
    metadata = item.metadata or {}
    return (
        item.object_type == ApprovalQueueObjectType.OUTREACH_DRAFT
        and str(metadata.get("outreach_channel") or "").strip().lower() == "email"
    )


def _maybe_create_email_draft(
    item: Any,
    *,
    live_gmail: bool,
) -> dict[str, Any] | None:
    if not _is_email_outreach_item(item):
        return None
    metadata = item.metadata or {}
    recipient = str(metadata.get("recipient_email") or "").strip()
    subject, body = _parse_email_draft_text(item.draft_text or "")
    subject = str(metadata.get("email_subject") or subject).strip()
    if not recipient:
        raise ValueError("Approved email draft is missing recipient_email metadata.")
    if not subject:
        raise ValueError("Approved email draft is missing an email subject.")
    if not body:
        raise ValueError("Approved email draft is missing an email body.")
    return GmailTool(live=live_gmail).create_draft(
        to=recipient,
        subject=subject,
        body=body,
    )


def _parse_email_draft_text(draft_text: str) -> tuple[str, str]:
    text = draft_text.strip()
    subject = ""
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        text = rest.strip()
    body, _, _linkedin = text.partition("\n\nLinkedIn:")
    return subject, body.strip()


def _revision_prompt(item: Any, *, feedback: str) -> str:
    metadata = item.metadata or {}
    feedback_line = feedback or "[operator should reply in Slack with specific edits]"
    return "\n".join(
        [
            "Revise this approval-gated outreach draft using the operator feedback.",
            f"Approval ID: {item.id}",
            f"Company: {metadata.get('company_name') or 'unknown'}",
            f"Contact: {metadata.get('contact_name') or 'unknown'}",
            f"Channel: {metadata.get('outreach_channel') or item.object_type.value}",
            f"Destination: {_destination(metadata)}",
            f"Operator feedback: {feedback_line}",
            "",
            "Current draft:",
            item.draft_text or "",
            "",
            (
                "Return one revised draft only. Do not send, publish, schedule, "
                "or create a Gmail draft."
            ),
        ]
    ).strip()


def _rejection_feedback_prompt(item: Any) -> str:
    metadata = item.metadata or {}
    return (
        "Please reply with why this approval was rejected, for example: wrong target, "
        "weak company fit, bad timing, unsupported claim, too generic, too long, or "
        f"wrong tone. Approval ID: {item.id}; company: {metadata.get('company_name') or 'unknown'}."
    )


def _destination(metadata: dict[str, Any]) -> str:
    channel = str(metadata.get("outreach_channel") or "").strip().lower()
    if channel == "linkedin":
        return str(metadata.get("linkedin_url") or "needs confirmation")
    return str(metadata.get("recipient_email") or "needs confirmation")
