"""Shared Slack action IDs and payload contract for business-agent cards."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUSINESS_AGENT_ACTION_SCHEMA = "keystone.business_agent_action.v1"
BUSINESS_AGENT_SLACK_CONTRACT_SCHEMA = "keystone.business_agent_slack_contract.v1"
BUSINESS_AGENT_SLACK_CONTRACT_VERSION = "1"
BUSINESS_AGENT_SLACK_CONTRACT_CAPABILITIES = (
    "feedback_jsonl",
    "operator_failure_payload",
    "selected_context_prior_agent_runs",
    "selected_context_embedded_fallback",
    "write_gate_no_send",
)
SLACK_SELECTED_CONTEXT_SCHEMA = "keystone.slack.selected_message_context.v1"
SLACK_AGENT_FEEDBACK_EVENT_SCHEMA = "keystone.slack.agent_feedback_event.v1"
OPERATOR_FAILURE_SCHEMA = "keystone.operator_failure.v1"

KBA_CREATE_GMAIL_DRAFT = "kba_create_gmail_draft"
KBA_APPROVE_EXTERNAL_USE = "kba_approve_external_use"
KBA_REVISE_DRAFT = "kba_revise_draft"
KBA_MORE_RESEARCH = "kba_more_research"
KBA_FIND_CONTACT = "kba_find_contact"
KBA_OVERFLOW = "kba_overflow"
KBA_COS_AUDIT_AUTOMATIONS = "kba_cos_audit_automations"
KBA_COS_GENERATE_DOC = "kba_cos_generate_doc"
KBA_COS_SYNC_AIRTABLE = "kba_cos_sync_airtable"
KBA_COS_POST_SUMMARY = "kba_cos_post_summary"
KBA_COS_SHOW_BLOCKERS = "kba_cos_show_blockers"
KBA_COS_CONTINUE_WORK_ITEM = "kba_cos_continue_work_item"
KBA_OVERFLOW_ACTION_ID = KBA_OVERFLOW

LEGACY_APPROVAL_YES = "keystone_approval_yes"
LEGACY_APPROVAL_NO = "keystone_approval_no"
LEGACY_APPROVAL_REVISE = "keystone_approval_revise"
LEGACY_APPROVAL_ACTION_IDS = frozenset(
    {
        LEGACY_APPROVAL_YES,
        LEGACY_APPROVAL_NO,
        LEGACY_APPROVAL_REVISE,
    }
)

KBA_ACTION_IDS = frozenset(
    {
        KBA_CREATE_GMAIL_DRAFT,
        KBA_APPROVE_EXTERNAL_USE,
        KBA_REVISE_DRAFT,
        KBA_MORE_RESEARCH,
        KBA_FIND_CONTACT,
        KBA_OVERFLOW,
        KBA_COS_AUDIT_AUTOMATIONS,
        KBA_COS_GENERATE_DOC,
        KBA_COS_SYNC_AIRTABLE,
        KBA_COS_POST_SUMMARY,
        KBA_COS_SHOW_BLOCKERS,
        KBA_COS_CONTINUE_WORK_ITEM,
    }
)

KBA_INTENT_CREATE_GMAIL_DRAFT = "create_gmail_draft"
KBA_INTENT_APPROVE_EXTERNAL_USE = "approve_external_use"
KBA_INTENT_REVISE_DRAFT = "revise_draft"
KBA_INTENT_MORE_RESEARCH = "more_research"
KBA_INTENT_RESEARCH_ALL_CANDIDATES = "research_all_candidates"
KBA_INTENT_FIND_CONTACT = "find_contact"
KBA_INTENT_SHOW_SOURCES = "show_sources"
KBA_INTENT_OPEN_WORK_ITEM = "open_work_item"
KBA_INTENT_RUN_AGAIN = "run_again"
KBA_INTENT_SKIP_COMPANY = "skip_company"
KBA_INTENT_AUDIT_AUTOMATIONS = "audit_automations"
KBA_INTENT_GENERATE_DOC = "generate_doc"
KBA_INTENT_SYNC_AIRTABLE = "sync_airtable"
KBA_INTENT_POST_INTERNAL_SUMMARY = "post_internal_summary"
KBA_INTENT_SHOW_BLOCKERS = "show_blockers"
KBA_INTENT_CONTINUE_WORK_ITEM = "continue_work_item"
KBA_INTENTS = frozenset(
    {
        KBA_INTENT_CREATE_GMAIL_DRAFT,
        KBA_INTENT_APPROVE_EXTERNAL_USE,
        KBA_INTENT_REVISE_DRAFT,
        KBA_INTENT_MORE_RESEARCH,
        KBA_INTENT_RESEARCH_ALL_CANDIDATES,
        KBA_INTENT_FIND_CONTACT,
        KBA_INTENT_SHOW_SOURCES,
        KBA_INTENT_OPEN_WORK_ITEM,
        KBA_INTENT_RUN_AGAIN,
        KBA_INTENT_SKIP_COMPANY,
        KBA_INTENT_AUDIT_AUTOMATIONS,
        KBA_INTENT_GENERATE_DOC,
        KBA_INTENT_SYNC_AIRTABLE,
        KBA_INTENT_POST_INTERNAL_SUMMARY,
        KBA_INTENT_SHOW_BLOCKERS,
        KBA_INTENT_CONTINUE_WORK_ITEM,
    }
)

RUN_AGENT_MESSAGE_CALLBACK_ID = "keystone_run_agent_message"
RUN_AGENT_VIEW_CALLBACK_ID = "keystone_run_agent_submit"
RUN_AGENT_TASK_BLOCK_ID = "keystone_agent_task_block"
RUN_AGENT_TASK_ACTION_ID = "keystone_agent_task"
KBA_REVISE_DRAFT_VIEW_CALLBACK_ID = "kba_revise_draft_submit"
KBA_REVISION_FEEDBACK_BLOCK_ID = "kba_revision_feedback_block"
KBA_REVISION_FEEDBACK_ACTION_ID = "kba_revision_feedback"

BUSINESS_AGENT_WRITE_GATE_ACTION_ID = "kba_approve_write_request"
BUSINESS_AGENT_WRITE_GATE_SCHEMA = "keystone.business_agent_write_request.v1"
BUSINESS_AGENT_WRITE_GATE_INTENT = "approve_agent_write_request"
BUSINESS_AGENT_WRITE_GATE_NO_SEND_TEXT = (
    "This approval only lets the AI agent run this request. It does not allow sending email, "
    "posting externally, publishing, scheduling, or creating live drafts unless a later "
    "specific gate allows it."
)


class BusinessAgentActionPayload(BaseModel):
    """JSON value carried by first-class business-agent Slack buttons."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=BUSINESS_AGENT_ACTION_SCHEMA, alias="schema")
    intent: str
    work_item_id: str = ""
    approval_id: str = ""
    gate_scope: str = ""
    artifact_id: str = ""
    source_channel_id: str = ""
    source_message_ts: str = ""
    source_thread_ts: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "schema_name",
        "intent",
        "work_item_id",
        "approval_id",
        "gate_scope",
        "artifact_id",
        "source_channel_id",
        "source_message_ts",
        "source_thread_ts",
        mode="before",
    )
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @field_validator("schema_name")
    @classmethod
    def _validate_schema_name(cls, value: str) -> str:
        if value != BUSINESS_AGENT_ACTION_SCHEMA:
            raise ValueError(f"Unsupported business-agent action schema: {value or 'missing'}")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _clean_metadata(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}


def business_agent_action_value(
    *,
    intent: str,
    work_item_id: str = "",
    approval_id: str = "",
    gate_scope: str = "",
    artifact_id: str = "",
    source_channel_id: str = "",
    source_message_ts: str = "",
    source_thread_ts: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Return a compact JSON Slack action value."""

    payload = BusinessAgentActionPayload(
        intent=intent,
        work_item_id=work_item_id,
        approval_id=approval_id,
        gate_scope=gate_scope,
        artifact_id=artifact_id,
        source_channel_id=source_channel_id,
        source_message_ts=source_message_ts,
        source_thread_ts=source_thread_ts,
        metadata=metadata or {},
    )
    return json.dumps(
        payload.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        sort_keys=True,
    )


def parse_business_agent_action_value(
    value: Any,
    *,
    fallback_intent: str = "",
    fallback_approval_id: str = "",
) -> BusinessAgentActionPayload:
    """Parse a Slack action value, accepting legacy plain approval IDs as fallback."""

    raw = str(value or "").strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("business-agent Slack action value must be a JSON object")
        return BusinessAgentActionPayload.model_validate(data)
    return BusinessAgentActionPayload(
        intent=fallback_intent,
        approval_id=raw or fallback_approval_id,
    )


class SlackAgentFeedbackEvent(BaseModel):
    """JSONL-safe progress event emitted while a Slack agent run executes."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=SLACK_AGENT_FEEDBACK_EVENT_SCHEMA, alias="schema")
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_name", "event_type", mode="before")
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @field_validator("schema_name")
    @classmethod
    def _validate_schema_name(cls, value: str) -> str:
        if value != SLACK_AGENT_FEEDBACK_EVENT_SCHEMA:
            raise ValueError(f"Unsupported Slack agent feedback schema: {value or 'missing'}")
        return value

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        if not value:
            raise ValueError("Slack agent feedback event_type is required")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def _clean_payload(cls, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}


def slack_agent_feedback_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe Slack agent feedback event."""

    return SlackAgentFeedbackEvent(
        event_type=event_type,
        payload=payload,
    ).model_dump(mode="json", by_alias=True)


class OperatorFailurePayload(BaseModel):
    """Shared operator-readable failure shape embedded in Slack feedback and results."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=OPERATOR_FAILURE_SCHEMA, alias="schema")
    kind: str
    summary: str
    reason: str = ""
    next_step: str
    retryable: bool = False
    safe_to_continue: bool = False

    @field_validator("schema_name", "kind", "summary", "reason", "next_step", mode="before")
    @classmethod
    def _clean_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @field_validator("schema_name")
    @classmethod
    def _validate_schema_name(cls, value: str) -> str:
        if value != OPERATOR_FAILURE_SCHEMA:
            raise ValueError(f"Unsupported operator failure schema: {value or 'missing'}")
        return value


class BusinessAgentWriteGatePayload(BaseModel):
    """JSON value carried by Slack's business-agent write request gate."""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(default=BUSINESS_AGENT_WRITE_GATE_SCHEMA, alias="schema")
    intent: str = BUSINESS_AGENT_WRITE_GATE_INTENT
    request_text: str
    source_channel_id: str = ""
    source_thread_ts: str = ""
    source_request_ts: str = ""
    source_user_id: str = ""
    request_truncated: bool = False

    @field_validator(
        "schema_name",
        "intent",
        "request_text",
        "source_channel_id",
        "source_thread_ts",
        "source_request_ts",
        "source_user_id",
        mode="before",
    )
    @classmethod
    def _clean_write_gate_scalar(cls, value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @field_validator("schema_name")
    @classmethod
    def _validate_write_gate_schema(cls, value: str) -> str:
        if value != BUSINESS_AGENT_WRITE_GATE_SCHEMA:
            raise ValueError(f"Unsupported business-agent write-gate schema: {value or 'missing'}")
        return value

    @field_validator("intent")
    @classmethod
    def _validate_write_gate_intent(cls, value: str) -> str:
        if value != BUSINESS_AGENT_WRITE_GATE_INTENT:
            raise ValueError(f"Unsupported business-agent write-gate intent: {value or 'missing'}")
        return value

    @field_validator("request_text")
    @classmethod
    def _validate_write_gate_request(cls, value: str) -> str:
        if not value:
            raise ValueError("business-agent write gate requires request_text")
        return value


def business_agent_write_gate_value(
    *,
    request_text: str,
    source_channel_id: str = "",
    source_thread_ts: str = "",
    source_request_ts: str = "",
    source_user_id: str = "",
    request_truncated: bool = False,
) -> str:
    """Return a compact JSON Slack value for a write-capable agent request."""

    payload = BusinessAgentWriteGatePayload(
        request_text=request_text,
        source_channel_id=source_channel_id,
        source_thread_ts=source_thread_ts,
        source_request_ts=source_request_ts,
        source_user_id=source_user_id,
        request_truncated=request_truncated,
    )
    return json.dumps(
        payload.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_business_agent_write_gate_value(value: Any) -> BusinessAgentWriteGatePayload:
    """Parse and validate a business-agent write-gate Slack action value."""

    data = json.loads(str(value or "").strip())
    if not isinstance(data, dict):
        raise ValueError("business-agent write-gate value must be a JSON object")
    return BusinessAgentWriteGatePayload.model_validate(data)


def selected_message_context_json_schema() -> dict[str, Any]:
    """Return the canonical selected Slack context JSON schema."""

    from keystone_agents.slack_actions import SlackSelectedMessageContext

    return SlackSelectedMessageContext.model_json_schema()


def business_agent_slack_contract() -> dict[str, Any]:
    """Return the side-effect-free Slack action/context contract metadata."""

    return {
        "schema": BUSINESS_AGENT_SLACK_CONTRACT_SCHEMA,
        "version": BUSINESS_AGENT_SLACK_CONTRACT_VERSION,
        "capabilities": list(BUSINESS_AGENT_SLACK_CONTRACT_CAPABILITIES),
        "schemas": {
            "business_agent_action": BUSINESS_AGENT_ACTION_SCHEMA,
            "agent_feedback_event": SLACK_AGENT_FEEDBACK_EVENT_SCHEMA,
            "operator_failure": OPERATOR_FAILURE_SCHEMA,
            "selected_message_context": SLACK_SELECTED_CONTEXT_SCHEMA,
            "write_gate": BUSINESS_AGENT_WRITE_GATE_SCHEMA,
        },
        "action_ids": sorted(KBA_ACTION_IDS),
        "legacy_action_ids": sorted(LEGACY_APPROVAL_ACTION_IDS),
        "intents": sorted(KBA_INTENTS),
        "callback_ids": {
            "run_agent_message": RUN_AGENT_MESSAGE_CALLBACK_ID,
            "run_agent_view": RUN_AGENT_VIEW_CALLBACK_ID,
            "revise_draft_view": KBA_REVISE_DRAFT_VIEW_CALLBACK_ID,
        },
        "block_ids": {
            "run_agent_task": RUN_AGENT_TASK_BLOCK_ID,
            "revision_feedback": KBA_REVISION_FEEDBACK_BLOCK_ID,
        },
        "input_action_ids": {
            "run_agent_task": RUN_AGENT_TASK_ACTION_ID,
            "revision_feedback": KBA_REVISION_FEEDBACK_ACTION_ID,
            "write_gate": BUSINESS_AGENT_WRITE_GATE_ACTION_ID,
        },
        "write_gate": {
            "action_id": BUSINESS_AGENT_WRITE_GATE_ACTION_ID,
            "intent": BUSINESS_AGENT_WRITE_GATE_INTENT,
            "no_send_text": BUSINESS_AGENT_WRITE_GATE_NO_SEND_TEXT,
        },
        "payload_json_schemas": {
            "business_agent_action": BusinessAgentActionPayload.model_json_schema(),
            "agent_feedback_event": SlackAgentFeedbackEvent.model_json_schema(),
            "operator_failure": OperatorFailurePayload.model_json_schema(),
            "selected_message_context": selected_message_context_json_schema(),
            "write_gate": BusinessAgentWriteGatePayload.model_json_schema(),
        },
        "notes": [
            "Legacy approval action IDs are supported for compatibility only.",
            (
                "The contract is side-effect free and does not authorize Slack, Gmail, "
                "calendar, CRM, or external writes."
            ),
        ],
    }
