"""Human approval request helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from keystone_agents.guardrails import (
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.schemas.approval import (
    ApprovalQueueItem,
    ApprovalQueueObjectType,
    ApprovalQueueStatus,
    ApprovalRequest,
    ApprovalScope,
    ApprovalState,
    approved_state_for_scope,
    normalize_approval_queue_object_type,
    normalize_approval_queue_status,
    normalize_approval_scope,
    normalize_approval_state,
    queue_status_for_decision,
    state_allows_drafting,
    state_allows_external_use,
    state_allows_research,
    state_allows_sending,
)
from keystone_agents.sdk import function_tool
from keystone_agents.tools.slack_tool import SlackTool, slack_review_message_from_approval_request


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Expected draft mapping or Pydantic model, got {type(value).__name__}.")


def _risk_flags(payload: dict[str, Any], context: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    for source in (
        context.get("risk_flags"),
        payload.get("risk_flags"),
        payload.get("unsupported_claims_flagged"),
    ):
        if isinstance(source, list):
            flags.extend(str(item) for item in source if str(item).strip())
    return list(dict.fromkeys(flags))


def _object_type(payload: dict[str, Any], context: dict[str, Any]) -> str:
    if context.get("object_type"):
        return str(context["object_type"])
    if payload.get("draft_reply") is not None or payload.get("message_id"):
        return "gmail_draft"
    if payload.get("email_body") is not None or payload.get("linkedin_note") is not None:
        return "outreach_draft"
    return "draft"


def _queue_object_type(payload: dict[str, Any], context: dict[str, Any]) -> ApprovalQueueObjectType:
    try:
        return normalize_approval_queue_object_type(_object_type(payload, context))
    except ValueError:
        return ApprovalQueueObjectType.OTHER


def _object_id(payload: dict[str, Any], context: dict[str, Any]) -> str | None:
    for key in ("object_id", "message_id", "id", "company_name"):
        value = context.get(key) if key in context else payload.get(key)
        if value not in {None, ""}:
            return str(value)
    return None


def _summary(payload: dict[str, Any], context: dict[str, Any]) -> str:
    for key in ("summary", "subject", "email_subject", "recommended_action", "company_name"):
        value = context.get(key) if key in context else payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Draft approval requested."


def _title(payload: dict[str, Any], context: dict[str, Any]) -> str:
    value = context.get("title") or payload.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    summary = _summary(payload, context)
    return summary[:120] if len(summary) > 120 else summary


def _draft_text(payload: dict[str, Any], context: dict[str, Any]) -> str:
    if isinstance(context.get("draft_text"), str):
        return str(context["draft_text"]).strip()
    if isinstance(payload.get("draft_reply"), str):
        return str(payload["draft_reply"]).strip()

    object_type = _object_type(payload, context)
    subject = str(payload.get("email_subject") or payload.get("subject") or "").strip()
    body = str(payload.get("email_body") or "").strip()
    if not body and object_type == "outreach_draft":
        body = str(payload.get("body") or "").strip()
    linkedin = str(payload.get("linkedin_note") or "").strip()

    parts: list[str] = []
    if subject:
        parts.append(f"Subject: {subject}")
    if body:
        parts.append(body)
    if linkedin:
        parts.append(f"LinkedIn: {linkedin}")
    return "\n\n".join(parts)


def _source_agent(payload: dict[str, Any], context: dict[str, Any]) -> str:
    for key in ("source_agent", "agent_name"):
        value = context.get(key) if key in context else payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "approval_tool"


def _approval_decision(context: dict[str, Any]) -> ApprovalState:
    for key in ("decision", "approval_state", "approval_decision"):
        if key in context:
            return normalize_approval_state(context.get(key))
    if "approval_status" in context:
        try:
            return normalize_approval_state(context.get("approval_status"))
        except ValueError:
            return ApprovalState.PENDING
    return ApprovalState.PENDING


def _approval_queue_status(
    context: dict[str, Any],
    decision: ApprovalState,
) -> ApprovalQueueStatus:
    for key in ("queue_status", "approval_queue_status", "status"):
        if key in context:
            return normalize_approval_queue_status(context.get(key))
    return queue_status_for_decision(decision)


def _approval_scope(payload: dict[str, Any], context: dict[str, Any]) -> ApprovalScope:
    for key in ("scope", "approval_scope"):
        if key in context:
            return normalize_approval_scope(context.get(key))
    object_type = _object_type(payload, context)
    if object_type in {"company", "company_profile", "opportunity", "opportunity_record"}:
        return ApprovalScope.DRAFTING
    if object_type == "outreach_draft":
        return ApprovalScope.EXTERNAL_USE
    return ApprovalScope.SEND


def _slack_text(request: ApprovalRequest) -> str:
    return slack_review_message_from_approval_request(request).root_text


def build_approval_queue_item(
    draft: Any,
    context: Mapping[str, Any] | None = None,
) -> ApprovalQueueItem:
    """Build a local approval queue item without posting or sending anything."""

    payload = _as_dict(draft)
    context_dict = dict(context or {})
    enforce_tool_input_guardrails(
        "approval_build_approval_queue_item",
        {"draft": payload, "context": context_dict},
    )
    decision = _approval_decision(context_dict)
    status = _approval_queue_status(context_dict, decision)
    draft_text = (
        None
        if status
        in {
            ApprovalQueueStatus.REJECTED,
            ApprovalQueueStatus.EXPIRED,
            ApprovalQueueStatus.ARCHIVED,
        }
        else _draft_text(payload, context_dict) or None
    )
    item_kwargs: dict[str, Any] = {
        "object_type": _queue_object_type(payload, context_dict),
        "object_id": _object_id(payload, context_dict),
        "title": _title(payload, context_dict),
        "summary": _summary(payload, context_dict),
        "draft_text": draft_text,
        "source_agent": _source_agent(payload, context_dict),
        "risk_flags": _risk_flags(payload, context_dict),
        "approval_status": status,
        "reviewer": context_dict.get("reviewer") or None,
        "reviewer_notes": context_dict.get("notes") or None,
        "expires_at": context_dict.get("expires_at"),
        "metadata": dict(context_dict.get("metadata") or {}),
    }
    item_id = str(context_dict.get("approval_id") or payload.get("approval_id") or "").strip()
    if item_id:
        item_kwargs["id"] = item_id
    item = ApprovalQueueItem(**item_kwargs)
    return enforce_tool_output_guardrails("approval_build_approval_queue_item", item)


def _json_mapping(value: str, *, label: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object.")
    return payload


@function_tool(**keystone_tool_guardrail_kwargs())
def create_approval_queue_item(draft_json: str, context_json: str = "{}") -> str:
    """Build a local approval queue item as JSON without saving, posting, or sending."""

    item = build_approval_queue_item(
        _json_mapping(draft_json, label="draft_json"),
        context=_json_mapping(context_json, label="context_json"),
    )
    return json.dumps(
        {
            "mode": "local",
            "saved": False,
            "posted": False,
            "send_enabled": False,
            "item": item.model_dump(mode="json"),
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def post_approval_request(
    draft: Any,
    context: Mapping[str, Any] | None = None,
    live: bool = False,
) -> ApprovalRequest:
    """Create an approval request and optionally post it to Slack."""

    payload = _as_dict(draft)
    context_dict = dict(context or {})
    enforce_tool_input_guardrails(
        "approval_post_approval_request",
        {"draft": payload, "context": context_dict, "live": live},
    )
    decision = _approval_decision(context_dict)
    scope = _approval_scope(payload, context_dict)
    draft_text = (
        ""
        if decision in {ApprovalState.REJECTED, ApprovalState.EXPIRED}
        else _draft_text(payload, context_dict)
    )
    request = ApprovalRequest(
        object_type=_object_type(payload, context_dict),
        object_id=_object_id(payload, context_dict),
        summary=_summary(payload, context_dict),
        draft_text=draft_text,
        risk_flags=_risk_flags(payload, context_dict),
        decision=decision,
        scope=scope,
        reviewer=str(context_dict.get("reviewer") or ""),
        notes=str(context_dict.get("notes") or ""),
    )

    if decision in {ApprovalState.REJECTED, ApprovalState.EXPIRED}:
        return enforce_tool_output_guardrails("approval_post_approval_request", request)

    slack_result = SlackTool(live=live).post_message(
        channel=context_dict.get("slack_channel"),
        text=_slack_text(request),
    )
    output = request.model_copy(update={"slack_ts": slack_result.get("ts")})
    return enforce_tool_output_guardrails("approval_post_approval_request", output)


@dataclass(frozen=True)
class ApprovalTool:
    auto_approve: bool = False
    scope: ApprovalScope = ApprovalScope.DRAFTING

    def request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        scope = normalize_approval_scope(
            payload.get("approval_scope") or payload.get("scope") or self.scope
        )
        decision = approved_state_for_scope(scope) if self.auto_approve else ApprovalState.PENDING
        return {
            "action": action,
            "payload": payload,
            "decision": decision.value,
            "scope": scope.value,
            "can_research": state_allows_research(decision),
            "can_draft": state_allows_drafting(decision),
            "can_external_use": state_allows_external_use(decision),
            "can_send": state_allows_sending(decision),
        }

    def post_approval_request(
        self,
        draft: Any,
        context: Mapping[str, Any] | None = None,
        live: bool = False,
    ) -> ApprovalRequest:
        return post_approval_request(draft, context=context, live=live)

    def build_queue_item(
        self,
        draft: Any,
        context: Mapping[str, Any] | None = None,
    ) -> ApprovalQueueItem:
        return build_approval_queue_item(draft, context=context)
