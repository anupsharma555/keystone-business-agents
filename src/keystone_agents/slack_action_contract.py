"""Shared Slack action IDs and payload contract for business-agent cards."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

BUSINESS_AGENT_ACTION_SCHEMA = "keystone.business_agent_action.v1"

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

KBA_REVISE_DRAFT_VIEW_CALLBACK_ID = "kba_revise_draft_submit"


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
