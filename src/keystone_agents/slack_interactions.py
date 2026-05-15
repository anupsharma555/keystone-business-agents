"""Slack interactive approval handling for local Keystone approval items."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs

from pydantic import BaseModel

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.automation_inventory import build_automation_inventory_report
from keystone_agents.langgraph_workflow import advance_work_item_with_optional_langgraph
from keystone_agents.natural_interaction import resolve_natural_followup
from keystone_agents.schemas.approval import ApprovalQueueObjectType, ApprovalQueueStatus
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItemBlocker,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.slack_action_contract import (
    KBA_ACTION_IDS,
    KBA_APPROVE_EXTERNAL_USE,
    KBA_COS_AUDIT_AUTOMATIONS,
    KBA_COS_CONTINUE_WORK_ITEM,
    KBA_COS_GENERATE_DOC,
    KBA_COS_POST_SUMMARY,
    KBA_COS_SHOW_BLOCKERS,
    KBA_COS_SYNC_AIRTABLE,
    KBA_CREATE_GMAIL_DRAFT,
    KBA_FIND_CONTACT,
    KBA_INTENT_APPROVE_EXTERNAL_USE,
    KBA_INTENT_AUDIT_AUTOMATIONS,
    KBA_INTENT_CONTINUE_WORK_ITEM,
    KBA_INTENT_CREATE_GMAIL_DRAFT,
    KBA_INTENT_FIND_CONTACT,
    KBA_INTENT_GENERATE_DOC,
    KBA_INTENT_MORE_RESEARCH,
    KBA_INTENT_OPEN_WORK_ITEM,
    KBA_INTENT_POST_INTERNAL_SUMMARY,
    KBA_INTENT_REVISE_DRAFT,
    KBA_INTENT_RUN_AGAIN,
    KBA_INTENT_SHOW_BLOCKERS,
    KBA_INTENT_SHOW_SOURCES,
    KBA_INTENT_SKIP_COMPANY,
    KBA_INTENT_SYNC_AIRTABLE,
    KBA_MORE_RESEARCH,
    KBA_OVERFLOW,
    KBA_REVISE_DRAFT,
    KBA_REVISE_DRAFT_VIEW_CALLBACK_ID,
    BusinessAgentActionPayload,
    parse_business_agent_action_value,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.operations_publisher_tool import (
    publish_document_report_impl,
    publish_slack_summary_impl,
    publish_table_mirror_impl,
)
from keystone_agents.work_items import (
    add_blocker,
    apply_slack_approval_to_work_item_gate,
    record_event,
    select_artifact,
    set_next_action,
)

ACTION_STATUS_BY_ID = {
    "keystone_approval_yes": ApprovalQueueStatus.APPROVED,
    "keystone_approval_no": ApprovalQueueStatus.REJECTED,
    "keystone_approval_revise": ApprovalQueueStatus.REVISE,
}
KBA_REVISION_FEEDBACK_BLOCK_ID = "kba_revision_feedback_block"
KBA_REVISION_FEEDBACK_ACTION_ID = "kba_revision_feedback"
DEFAULT_GMAIL_DRAFT_ACCOUNT = "wisegrow05@gmail.com"
GMAIL_DRAFT_ACCOUNT_ENV_KEYS = (
    "KEYSTONE_GMAIL_DRAFT_ACCOUNT",
    "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
)


class SlackApprovalInteractionResult(BaseModel):
    """Result of applying one Slack approval action locally."""

    approval_id: str
    action_id: str
    approval_status: ApprovalQueueStatus
    stage: str = "approval"
    object_type: str = ""
    object_id: str = ""
    source_agent: str = ""
    reviewer: str = ""
    notes: str = ""
    outcome: str = ""
    followup_text: str = ""
    modal_view: dict[str, Any] | None = None
    slack_view_response_payload: dict[str, Any] | None = None
    queued_route: str = ""
    queued_command: str = ""
    read_only_payload: dict[str, Any] | None = None
    gmail_draft_result: dict[str, Any] | None = None
    revision_prompt: str = ""
    feedback_prompt: str = ""
    work_item_id: str = ""
    work_item_status: str = ""
    work_item_gate_scope: str = ""
    work_item_gate_state: str = ""
    work_item_gate_changed: bool = False
    work_item_event_recorded: bool = False
    idempotent: bool = False
    slack_channel_id: str = ""
    slack_message_ts: str = ""
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
    create_email_draft: bool = False,
    live_gmail: bool = False,
) -> SlackApprovalInteractionResult:
    """Apply a Slack approval button action to the local approval queue."""

    payload = parse_slack_interaction_payload(raw_payload)
    if _is_kba_revision_modal_submission(payload):
        return _handle_kba_revision_modal_submission(payload, database_url=database_url)
    action = _first_action(payload)
    action_id = str(action.get("action_id") or "").strip()
    if action_id in KBA_ACTION_IDS:
        return _handle_kba_action(
            payload,
            action,
            action_id=action_id,
            database_url=database_url,
            create_email_draft=create_email_draft,
            live_gmail=live_gmail,
        )
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
    existing_item = store.get_approval_item(approval_id)
    if existing_item is None:
        raise KeyError(f"Approval queue item not found: {approval_id}")
    linked_work_item = _linked_work_item(store, existing_item)
    previous_queue_status = existing_item.approval_status
    item = store.update_approval_status(
        approval_id,
        status,
        reviewer=reviewer,
        notes=notes,
    )
    metadata = dict(item.metadata or {})
    if feedback:
        metadata["operator_feedback_for_agent"] = feedback
        metadata["operator_feedback_source"] = "slack_interaction"
        metadata["operator_feedback_status"] = status.value
        metadata["feedback_available_for_llm"] = True
    gmail_draft_result: dict[str, Any] | None = None
    revision_prompt = ""
    feedback_prompt = ""
    followup_text = _followup_text_for_status(status, item, feedback=feedback)
    slack_context = _slack_context(payload)
    gate_update = None

    if linked_work_item is not None:
        gate_update = apply_slack_approval_to_work_item_gate(
            linked_work_item,
            approval_id,
            status,
            actor=reviewer,
            notes=notes,
            slack_context=slack_context,
            store=store,
        )
        metadata["work_item_id"] = linked_work_item.id
        metadata["work_item_gate_scope"] = gate_update.gate_scope
        metadata["work_item_gate_state"] = gate_update.new_state
        metadata["work_item_gate_changed_from"] = gate_update.previous_state
        metadata["work_item_gate_changed"] = gate_update.changed
        metadata["work_item_event_recorded"] = gate_update.event_recorded
        metadata["slack_action_context"] = slack_context
        item = _save_metadata(store, item, metadata)
        followup_text = _work_item_followup_text(status, gate_update, feedback=feedback)

    if (
        status == ApprovalQueueStatus.APPROVED
        and create_email_draft
        and _slack_draft_creation_allowed(item)
    ):
        if previous_queue_status == status:
            gmail_draft_result = _duplicate_gmail_draft_result(item)
            followup_text = (
                "Duplicate Slack approval ignored. No additional Gmail draft was created."
            )
        else:
            gmail_draft_result = _maybe_create_email_draft(item, live_gmail=live_gmail)
        if gmail_draft_result is not None:
            if gmail_draft_result.get("status") != "duplicate_ignored":
                metadata["gmail_draft_result"] = gmail_draft_result
                metadata["gmail_draft_created_from_slack_approval"] = bool(
                    gmail_draft_result.get("status") == "draft_created"
                )
            else:
                metadata["gmail_draft_duplicate_ignored"] = True
            item = _save_metadata(store, item, metadata)
            draft_status = str(gmail_draft_result.get("status") or "").strip()
            if draft_status == "duplicate_ignored":
                followup_text = (
                    "Duplicate Slack approval ignored. No additional Gmail draft was created."
                )
            elif draft_status == "draft_created":
                account = _gmail_draft_account_text(gmail_draft_result)
                followup_text = (
                    f"Approved. Gmail draft was created{account} for human review; "
                    "no email was sent."
                )
            elif draft_status == "dry-run":
                account = _gmail_draft_account_text(gmail_draft_result)
                followup_text = (
                    f"Approved. Gmail draft creation{account} returned a dry-run preview; "
                    "no email was sent."
                )
            elif draft_status == "skipped":
                reason = str(
                    gmail_draft_result.get("reason") or "missing required draft metadata"
                )
                followup_text = (
                    f"Approved. Gmail draft was not created: {reason}; no email was sent."
                )
            else:
                followup_text = "Approved. Gmail draft action completed; no email was sent."
    elif (
        status == ApprovalQueueStatus.APPROVED
        and create_email_draft
        and _is_email_outreach_item(item)
    ):
        metadata["gmail_draft_result"] = {
            "status": "skipped",
            "reason": "Slack approval records the gate only; use the explicit draft command.",
            "send_enabled": False,
        }
        item = _save_metadata(store, item, metadata)
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

    if feedback and (item.metadata or {}).get("operator_feedback_for_agent") != feedback:
        item = _save_metadata(store, item, metadata)

    return SlackApprovalInteractionResult(
        approval_id=item.id,
        action_id=action_id,
        approval_status=item.approval_status,
        object_type=item.object_type,
        object_id=item.object_id or "",
        source_agent=item.source_agent,
        reviewer=item.reviewer or reviewer,
        notes=item.reviewer_notes or notes,
        outcome=_outcome_label(status, item),
        followup_text=followup_text,
        gmail_draft_result=gmail_draft_result,
        revision_prompt=revision_prompt,
        feedback_prompt=feedback_prompt,
        work_item_id=gate_update.work_item.id if gate_update else "",
        work_item_status=gate_update.work_item.status.value if gate_update else "",
        work_item_gate_scope=gate_update.gate_scope if gate_update else "",
        work_item_gate_state=gate_update.new_state if gate_update else "",
        work_item_gate_changed=bool(gate_update and gate_update.changed),
        work_item_event_recorded=bool(gate_update and gate_update.event_recorded),
        idempotent=previous_queue_status == status and not bool(
            gate_update and gate_update.changed
        ),
        slack_channel_id=slack_context.get("channel_id", ""),
        slack_message_ts=slack_context.get("message_ts", ""),
    )


def _handle_kba_action(
    payload: dict[str, Any],
    action: dict[str, Any],
    *,
    action_id: str,
    database_url: str | None,
    create_email_draft: bool,
    live_gmail: bool,
) -> SlackApprovalInteractionResult:
    action_payload = _kba_action_payload(payload, action, action_id=action_id)
    store = SQLiteStore(database_url or database_url_from_env())
    if action_id in {KBA_CREATE_GMAIL_DRAFT, KBA_APPROVE_EXTERNAL_USE}:
        _assert_first_class_action_targets_current_gate(store, action_payload)
        legacy = _legacy_approval_payload(
            payload,
            action_payload,
            legacy_action_id="keystone_approval_yes",
        )
        result = handle_slack_approval_interaction(
            legacy,
            database_url=database_url,
            create_email_draft=(
                action_id == KBA_CREATE_GMAIL_DRAFT or create_email_draft
            ),
            live_gmail=live_gmail,
        )
        outcome = (
            "draft_created"
            if action_id == KBA_CREATE_GMAIL_DRAFT
            and result.gmail_draft_result
            and result.gmail_draft_result.get("status") == "draft_created"
            else result.outcome
        )
        return result.model_copy(
            update={
                "action_id": action_id,
                "stage": "approval",
                "outcome": outcome,
            }
        )
    if action_id == KBA_REVISE_DRAFT:
        item = _assert_first_class_action_targets_current_gate(store, action_payload)
        return SlackApprovalInteractionResult(
            approval_id=action_payload.approval_id,
            action_id=action_id,
            approval_status=item.approval_status,
            stage="modal",
            object_type=item.object_type,
            object_id=item.object_id or "",
            source_agent=item.source_agent,
            reviewer=_reviewer(payload),
            outcome="revision_modal_opened",
            followup_text="Opening revision instructions modal.",
            modal_view=_build_revision_modal(action_payload, item),
            work_item_id=(
                action_payload.work_item_id
                or str((item.metadata or {}).get("work_item_id") or "")
            ),
            slack_channel_id=action_payload.source_channel_id,
            slack_message_ts=action_payload.source_message_ts,
        )
    return _handle_kba_steering_action(
        store,
        payload,
        action_payload,
        action_id=action_id,
        database_url=database_url,
    )


def _handle_kba_revision_modal_submission(
    payload: dict[str, Any],
    *,
    database_url: str | None,
) -> SlackApprovalInteractionResult:
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    action_payload = parse_business_agent_action_value(view.get("private_metadata") or "{}")
    feedback = _feedback_text({"state": view.get("state")})
    store = SQLiteStore(database_url or database_url_from_env())
    item = _assert_first_class_action_targets_current_gate(store, action_payload)
    if not feedback:
        return SlackApprovalInteractionResult(
            approval_id=action_payload.approval_id,
            action_id=KBA_REVISE_DRAFT,
            approval_status=item.approval_status,
            stage="modal_error",
            object_type=item.object_type,
            object_id=item.object_id or "",
            reviewer=_reviewer(payload),
            outcome="revision_feedback_required",
            followup_text="Revision instructions are required.",
            slack_view_response_payload={
                "response_action": "errors",
                "errors": {
                    KBA_REVISION_FEEDBACK_BLOCK_ID: "Tell the agent what to change."
                },
            },
            work_item_id=action_payload.work_item_id,
            slack_channel_id=action_payload.source_channel_id,
            slack_message_ts=action_payload.source_message_ts,
        )

    legacy = _legacy_approval_payload(
        payload,
        action_payload,
        legacy_action_id="keystone_approval_revise",
        feedback=feedback,
    )
    result = handle_slack_approval_interaction(legacy, database_url=database_url)
    queued_route = ""
    queued_command = ""
    view_response_text = result.followup_text
    work_item_id = action_payload.work_item_id or result.work_item_id
    if work_item_id:
        advance = _advance_work_item_for_intent(
            work_item_id=work_item_id,
            intent=KBA_INTENT_REVISE_DRAFT,
            reviewer=result.reviewer,
            feedback=feedback,
            database_url=database_url,
        )
        queued_route = advance.route.value
        queued_command = _queued_command(work_item_id, queued_route)
        view_response_text = (
            f"Revision requested. Outreach Composer was queued for WorkItem `{work_item_id}`."
        )
    return result.model_copy(
        update={
            "action_id": KBA_REVISE_DRAFT,
            "stage": "work_item",
            "outcome": "revision_requested",
            "queued_route": queued_route,
            "queued_command": queued_command,
            "followup_text": view_response_text,
            "slack_view_response_payload": _modal_update_response(
                title="Revision queued",
                text=view_response_text,
            ),
        }
    )


def _handle_kba_steering_action(
    store: SQLiteStore,
    payload: dict[str, Any],
    action_payload: BusinessAgentActionPayload,
    *,
    action_id: str,
    database_url: str | None,
) -> SlackApprovalInteractionResult:
    item = _approval_item_for_action(store, action_payload)
    status = item.approval_status if item is not None else ApprovalQueueStatus.PENDING
    reviewer = _reviewer(payload)
    intent = action_payload.intent
    if action_id == KBA_OVERFLOW:
        intent = intent or KBA_INTENT_SHOW_SOURCES
    if intent in {
        KBA_INTENT_AUDIT_AUTOMATIONS,
        KBA_INTENT_GENERATE_DOC,
        KBA_INTENT_SYNC_AIRTABLE,
        KBA_INTENT_POST_INTERNAL_SUMMARY,
        KBA_INTENT_SHOW_BLOCKERS,
        KBA_INTENT_CONTINUE_WORK_ITEM,
    }:
        return _handle_chief_of_staff_action(
            store,
            payload,
            action_payload,
            intent=intent,
            action_id=action_id,
            reviewer=reviewer,
            database_url=database_url,
        )
    if intent == KBA_INTENT_SHOW_SOURCES:
        work_item = _optional_work_item_for_action(store, action_payload, item)
        read_only = _sources_payload(item, work_item)
        return _kba_result(
            action_payload,
            action_id=action_id,
            approval_status=status,
            reviewer=reviewer,
            outcome="sources_ready",
            followup_text=_sources_followup_text(read_only),
            item=item,
            work_item=work_item,
            read_only_payload=read_only,
        )
    work_item = _required_work_item_for_action(store, action_payload, item)
    if intent == KBA_INTENT_OPEN_WORK_ITEM:
        read_only = {
            "work_item_id": work_item.id,
            "status": work_item.status.value,
            "current_route": work_item.current_route.value,
            "next_action": work_item.next_action.model_dump(mode="json")
            if work_item.next_action
            else None,
        }
        return _kba_result(
            action_payload,
            action_id=action_id,
            approval_status=status,
            reviewer=reviewer,
            outcome="work_item_ready",
            followup_text=(
                f"WorkItem `{work_item.id}` is `{work_item.status.value}` on "
                f"`{work_item.current_route.value}`."
            ),
            item=item,
            work_item=work_item,
            read_only_payload=read_only,
        )

    dedupe_key = _kba_dedupe_key(action_id, action_payload)
    if _has_recorded_kba_action(store, work_item.id, dedupe_key):
        return _kba_result(
            action_payload,
            action_id=action_id,
            approval_status=status,
            reviewer=reviewer,
            outcome=f"{intent}_duplicate_ignored",
            followup_text="Duplicate Slack action ignored. No additional agent step was queued.",
            item=item,
            work_item=work_item,
            idempotent=True,
        )

    if intent == KBA_INTENT_SKIP_COMPANY:
        updated = add_blocker(
            work_item,
            WorkItemBlocker(
                code="company_skipped_from_slack",
                message="Company candidate skipped from Slack action.",
            ),
        )
        updated = set_next_action(
            updated,
            WorkItemNextAction(
                action="select_different_company",
                description="Choose another company or run the opportunity scout again.",
            ),
        )
        updated = updated.model_copy(update={"status": WorkItemStatus.BLOCKED}).touch()
        _record_kba_action_event(
            store,
            updated,
            action_payload,
            action_id=action_id,
            reviewer=reviewer,
            dedupe_key=dedupe_key,
            summary="Slack action skipped this company candidate.",
        )
        store.save_work_item(updated)
        return _kba_result(
            action_payload,
            action_id=action_id,
            approval_status=status,
            reviewer=reviewer,
            outcome="skipped",
            followup_text=(
                f"Skipped. WorkItem `{updated.id}` remains blocked until another company "
                "or next step is selected."
            ),
            item=item,
            work_item=updated,
        )

    work_item = _select_action_artifact_if_present(store, work_item, action_payload)
    route = _route_for_steering_intent(intent, work_item)
    _record_kba_action_event(
        store,
        work_item,
        action_payload,
        action_id=action_id,
        reviewer=reviewer,
        dedupe_key=dedupe_key,
        summary=f"Slack action queued `{intent}` via `{route.value}`.",
    )
    advance = _advance_work_item_for_intent(
        work_item_id=work_item.id,
        intent=intent,
        reviewer=reviewer,
        feedback="",
        database_url=database_url,
        route=route,
    )
    outcome = {
        KBA_INTENT_MORE_RESEARCH: "research_queued",
        KBA_INTENT_FIND_CONTACT: "contact_search_queued",
        KBA_INTENT_RUN_AGAIN: "run_again_queued",
    }.get(intent, "agent_step_queued")
    followup = {
        KBA_INTENT_MORE_RESEARCH: "Research queued",
        KBA_INTENT_FIND_CONTACT: "Contact search queued",
        KBA_INTENT_RUN_AGAIN: "Run again queued",
    }.get(intent, "Agent step queued")
    return _kba_result(
        action_payload,
        action_id=action_id,
        approval_status=status,
        reviewer=reviewer,
        outcome=outcome,
        followup_text=f"{followup} for WorkItem `{work_item.id}`.",
        item=item,
        work_item=advance.work_item,
        queued_route=advance.route.value,
        queued_command=_queued_command(work_item.id, advance.route.value),
    )


def _select_action_artifact_if_present(
    store: SQLiteStore,
    work_item: Any,
    action_payload: BusinessAgentActionPayload,
) -> Any:
    artifact_ref = str(action_payload.artifact_id or "").strip()
    if ":" not in artifact_ref:
        return work_item
    artifact_type, artifact_id = artifact_ref.split(":", 1)
    artifact_type = artifact_type.strip()
    artifact_id = artifact_id.strip()
    if not artifact_type or not artifact_id:
        return work_item
    updated = select_artifact(work_item, artifact_type, artifact_id)
    record_event(
        updated,
        event_type="artifact_selected",
        actor="slack",
        summary=f"Selected {artifact_type}:{artifact_id} from Slack action.",
        metadata={"artifact_type": artifact_type, "artifact_id": artifact_id},
        store=store,
    )
    store.save_work_item(updated)
    return updated


def _first_action(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Slack interaction payload has no actions.")
    action = actions[0]
    if not isinstance(action, dict):
        raise ValueError("Slack interaction action must be an object.")
    return action


def _handle_chief_of_staff_action(
    store: SQLiteStore,
    payload: dict[str, Any],
    action_payload: BusinessAgentActionPayload,
    *,
    intent: str,
    action_id: str,
    reviewer: str,
    database_url: str | None,
) -> SlackApprovalInteractionResult:
    """Handle Chief of Staff operating-layer actions without approval ids."""

    slack_context = _slack_context(payload)
    channel = action_payload.source_channel_id or slack_context.get("channel_id", "")
    if intent == KBA_INTENT_CONTINUE_WORK_ITEM:
        work_item_id = action_payload.work_item_id
        if not work_item_id:
            resolution = resolve_natural_followup(
                "continue",
                database_url=database_url,
                metadata={"source": "slack_action"},
            )
            work_item_id = resolution.target_id
        if not work_item_id:
            return SlackApprovalInteractionResult(
                approval_id=action_payload.approval_id,
                action_id=action_id,
                approval_status=ApprovalQueueStatus.PENDING,
                stage="chief_of_staff",
                reviewer=reviewer,
                outcome="clarification_required",
                followup_text="No active WorkItem was found to continue.",
                slack_channel_id=channel,
                slack_message_ts=action_payload.source_message_ts,
            )
        advance = advance_work_item_with_optional_langgraph(
            WorkflowRunRequest(
                request_text="continue",
                work_item_id=work_item_id,
                save=True,
                database_url=database_url,
            )
        )
        return SlackApprovalInteractionResult(
            approval_id=action_payload.approval_id,
            action_id=action_id,
            approval_status=ApprovalQueueStatus.PENDING,
            stage="work_item",
            reviewer=reviewer,
            outcome="continued",
            followup_text=f"Continued WorkItem `{work_item_id}` via `{advance.route.value}`.",
            queued_route=advance.route.value,
            queued_command=_queued_command(work_item_id, advance.route.value),
            work_item_id=work_item_id,
            work_item_status=advance.status.value,
            slack_channel_id=channel,
            slack_message_ts=action_payload.source_message_ts,
        )

    report = build_automation_inventory_report(database_url=database_url)
    read_only_payload: dict[str, Any] = {"automation_report": report.model_dump(mode="json")}
    outcome = "automation_audit_ready"
    followup = f"Automation audit ready: {report.summary}"
    if intent == KBA_INTENT_GENERATE_DOC:
        read_only_payload["publish_result"] = publish_document_report_impl(
            report.model_dump_json(),
            destination="google_doc",
            live=False,
        )
        outcome = "document_report_ready"
        followup = "Dry-run Google Doc report artifact prepared."
    elif intent == KBA_INTENT_SYNC_AIRTABLE:
        read_only_payload["publish_result"] = publish_table_mirror_impl(
            report.model_dump_json(),
            destination="airtable",
            live=False,
        )
        outcome = "airtable_mirror_ready"
        followup = "Dry-run Airtable-shaped mirror rows prepared."
    elif intent == KBA_INTENT_POST_INTERNAL_SUMMARY:
        read_only_payload["publish_result"] = publish_slack_summary_impl(
            report.model_dump_json(),
            channel=channel or "#ai-agents-workflow",
            live=False,
        )
        outcome = "slack_summary_ready"
        followup = "Dry-run internal Slack summary prepared."
    elif intent == KBA_INTENT_SHOW_BLOCKERS:
        blockers = [
            finding.model_dump(mode="json")
            for finding in report.findings
            if finding.severity.value in {"warning", "error", "blocker"}
        ]
        read_only_payload = {
            "blockers": blockers,
            "pending_approval_count": report.pending_approval_count,
        }
        outcome = "blockers_ready"
        followup = f"Found {len(blockers)} blocker/warning item(s)."
    return SlackApprovalInteractionResult(
        approval_id=action_payload.approval_id,
        action_id=action_id,
        approval_status=ApprovalQueueStatus.PENDING,
        stage="chief_of_staff",
        reviewer=reviewer,
        outcome=outcome,
        followup_text=followup,
        read_only_payload=read_only_payload,
        slack_channel_id=channel,
        slack_message_ts=action_payload.source_message_ts,
    )


def _is_kba_revision_modal_submission(payload: dict[str, Any]) -> bool:
    if str(payload.get("type") or "").strip() != "view_submission":
        return False
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    return str(view.get("callback_id") or "").strip() == KBA_REVISE_DRAFT_VIEW_CALLBACK_ID


def _kba_action_payload(
    slack_payload: dict[str, Any],
    action: dict[str, Any],
    *,
    action_id: str,
) -> BusinessAgentActionPayload:
    fallback_intent = {
        KBA_CREATE_GMAIL_DRAFT: KBA_INTENT_CREATE_GMAIL_DRAFT,
        KBA_APPROVE_EXTERNAL_USE: KBA_INTENT_APPROVE_EXTERNAL_USE,
        KBA_REVISE_DRAFT: KBA_INTENT_REVISE_DRAFT,
        KBA_MORE_RESEARCH: KBA_INTENT_MORE_RESEARCH,
        KBA_FIND_CONTACT: KBA_INTENT_FIND_CONTACT,
        KBA_COS_AUDIT_AUTOMATIONS: KBA_INTENT_AUDIT_AUTOMATIONS,
        KBA_COS_GENERATE_DOC: KBA_INTENT_GENERATE_DOC,
        KBA_COS_SYNC_AIRTABLE: KBA_INTENT_SYNC_AIRTABLE,
        KBA_COS_POST_SUMMARY: KBA_INTENT_POST_INTERNAL_SUMMARY,
        KBA_COS_SHOW_BLOCKERS: KBA_INTENT_SHOW_BLOCKERS,
        KBA_COS_CONTINUE_WORK_ITEM: KBA_INTENT_CONTINUE_WORK_ITEM,
    }.get(action_id, "")
    selected = (
        action.get("selected_option") if isinstance(action.get("selected_option"), dict) else {}
    )
    raw_value = selected.get("value") if action_id == KBA_OVERFLOW else action.get("value")
    parsed = parse_business_agent_action_value(raw_value, fallback_intent=fallback_intent)
    slack_context = _slack_context(slack_payload)
    return parsed.model_copy(
        update={
            "source_channel_id": parsed.source_channel_id or slack_context.get("channel_id", ""),
            "source_message_ts": parsed.source_message_ts or slack_context.get("message_ts", ""),
            "source_thread_ts": parsed.source_thread_ts
            or slack_context.get("thread_ts", "")
            or slack_context.get("message_ts", ""),
        }
    )


def _assert_first_class_action_targets_current_gate(
    store: SQLiteStore,
    action_payload: BusinessAgentActionPayload,
) -> Any:
    if not action_payload.approval_id:
        raise ValueError("Business-agent Slack action is missing approval_id.")
    item = store.get_approval_item(action_payload.approval_id)
    if item is None:
        raise KeyError(f"Approval queue item not found: {action_payload.approval_id}")
    metadata = item.metadata or {}
    metadata_work_item_id = str(metadata.get("work_item_id") or "").strip()
    work_item = None
    if metadata_work_item_id:
        work_item = _linked_work_item(store, item)
    elif action_payload.work_item_id:
        work_item = store.get_work_item(action_payload.work_item_id)
        if work_item is None:
            raise KeyError(
                f"WorkItem not found for approval {item.id}: {action_payload.work_item_id}"
            )
        if not any(gate.approval_id == item.id for gate in work_item.approval_gates):
            raise ValueError(f"stale WorkItem approval id: {item.id}")
    if work_item is not None:
        if action_payload.work_item_id and action_payload.work_item_id != work_item.id:
            raise ValueError(
                f"Slack action WorkItem mismatch for approval {item.id}: "
                f"{action_payload.work_item_id} != {work_item.id}"
            )
        gate = next(gate for gate in work_item.approval_gates if gate.approval_id == item.id)
        if action_payload.gate_scope and action_payload.gate_scope != gate.scope:
            raise ValueError(
                f"Slack action gate scope mismatch for approval {item.id}: "
                f"{action_payload.gate_scope} != {gate.scope}"
            )
    return item


def _approval_item_for_action(
    store: SQLiteStore,
    action_payload: BusinessAgentActionPayload,
) -> Any | None:
    if not action_payload.approval_id:
        return None
    return _assert_first_class_action_targets_current_gate(store, action_payload)


def _optional_work_item_for_action(
    store: SQLiteStore,
    action_payload: BusinessAgentActionPayload,
    item: Any | None,
) -> Any | None:
    metadata_work_item_id = (
        str((item.metadata or {}).get("work_item_id") or "") if item is not None else ""
    )
    work_item_id = action_payload.work_item_id or metadata_work_item_id
    if not work_item_id:
        return None
    return store.get_work_item(work_item_id)


def _required_work_item_for_action(
    store: SQLiteStore,
    action_payload: BusinessAgentActionPayload,
    item: Any | None,
) -> Any:
    work_item = _optional_work_item_for_action(store, action_payload, item)
    if work_item is None:
        work_item_id = action_payload.work_item_id or str(
            (item.metadata or {}).get("work_item_id") if item is not None else ""
        )
        raise KeyError(f"WorkItem not found for Slack action: {work_item_id or 'missing'}")
    if action_payload.approval_id and not any(
        gate.approval_id == action_payload.approval_id for gate in work_item.approval_gates
    ):
        raise ValueError(f"stale WorkItem approval id: {action_payload.approval_id}")
    return work_item


def _legacy_approval_payload(
    payload: dict[str, Any],
    action_payload: BusinessAgentActionPayload,
    *,
    legacy_action_id: str,
    feedback: str = "",
) -> dict[str, Any]:
    legacy = dict(payload)
    if action_payload.source_channel_id:
        legacy["channel"] = {"id": action_payload.source_channel_id}
    if action_payload.source_message_ts:
        legacy["container"] = {
            "message_ts": action_payload.source_message_ts,
            "thread_ts": action_payload.source_thread_ts or action_payload.source_message_ts,
        }
    legacy["actions"] = [
        {
            "action_id": legacy_action_id,
            "value": action_payload.approval_id,
        }
    ]
    legacy.pop("type", None)
    legacy.pop("view", None)
    if feedback:
        legacy["state"] = {
            "values": {
                KBA_REVISION_FEEDBACK_BLOCK_ID: {
                    KBA_REVISION_FEEDBACK_ACTION_ID: {
                        "type": "plain_text_input",
                        "value": feedback,
                    }
                }
            }
        }
    return legacy


def _build_revision_modal(
    action_payload: BusinessAgentActionPayload,
    item: Any,
) -> dict[str, Any]:
    metadata = item.metadata or {}
    company = str(metadata.get("company_name") or "this draft").strip()
    summary = str(item.summary or item.title or "").strip()
    private_metadata = json.dumps(
        action_payload.model_dump(mode="json", by_alias=True),
        ensure_ascii=True,
        sort_keys=True,
    )
    return {
        "type": "modal",
        "callback_id": KBA_REVISE_DRAFT_VIEW_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Revise draft"},
        "submit": {"type": "plain_text", "text": "Queue revision"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private_metadata[:2900],
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"Revision target: `{company}`\n"
                        f"{summary[:500] or 'Add clear instructions for the next draft.'}"
                    ),
                },
            },
            {
                "type": "input",
                "block_id": KBA_REVISION_FEEDBACK_BLOCK_ID,
                "label": {"type": "plain_text", "text": "What should the agent change?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": KBA_REVISION_FEEDBACK_ACTION_ID,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Example: shorten the CTA and make the opening more specific.",
                    },
                },
            },
        ],
    }


def _advance_work_item_for_intent(
    *,
    work_item_id: str,
    intent: str,
    reviewer: str,
    feedback: str,
    database_url: str | None,
    route: WorkItemRoute | None = None,
) -> Any:
    resolved_route = route or _route_for_revision_intent(intent)
    request_text = _request_text_for_intent(intent, feedback=feedback)
    live_search = _slack_work_item_live_search_enabled()
    live_sdk = _slack_work_item_live_sdk_enabled(default=live_search)
    manual_plan = {
        "source": "slack_business_agent_action",
        "requested_agent": resolved_route.value,
        "target_agent": resolved_route.value,
        "intent": intent,
        "objective": request_text,
        "constraints": [feedback] if feedback else [],
        "requires_approved_context": True,
        "side_effect_policy": "draft_only_no_send_no_publish_no_schedule",
        "reviewer": reviewer,
    }
    return advance_work_item_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=request_text,
            work_item_id=work_item_id,
            save=True,
            database_url=database_url,
            live_search=live_search,
            live_sdk=live_sdk,
            requested_route=resolved_route,
            manual_request_plan=manual_plan,
        )
    )


def _slack_work_item_live_search_enabled() -> bool:
    return _env_truthy("KNI_BUSINESS_AGENTS_LIVE_SEARCH", default=False)


def _slack_work_item_live_sdk_enabled(*, default: bool) -> bool:
    explicit = os.environ.get("KNI_BUSINESS_AGENTS_WORKITEM_LIVE_SDK")
    if explicit is not None:
        return _truthy_string(explicit, default=default)
    return _env_truthy("KNI_BUSINESS_AGENTS_LIVE_SDK", default=default)


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return _truthy_string(raw, default=default)


def _truthy_string(raw: str, *, default: bool = False) -> bool:
    value = str(raw or "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return default


def _route_for_revision_intent(intent: str) -> WorkItemRoute:
    if intent == KBA_INTENT_REVISE_DRAFT:
        return WorkItemRoute.OUTREACH_COMPOSER
    return WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def _route_for_steering_intent(intent: str, work_item: Any) -> WorkItemRoute:
    if intent == KBA_INTENT_FIND_CONTACT:
        return WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    if intent == KBA_INTENT_MORE_RESEARCH:
        return WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    if intent == KBA_INTENT_RUN_AGAIN:
        route = work_item.current_route
        if route in {WorkItemRoute.ORCHESTRATOR, WorkItemRoute.CLARIFICATION}:
            return WorkItemRoute.BUSINESS_RESEARCH_ANALYST
        return route
    return WorkItemRoute.BUSINESS_RESEARCH_ANALYST


def _request_text_for_intent(intent: str, *, feedback: str) -> str:
    if intent == KBA_INTENT_REVISE_DRAFT:
        return f"Revise the outreach draft using this Slack feedback: {feedback}"
    if intent == KBA_INTENT_FIND_CONTACT:
        return "Find a better source-backed contact or destination for this outreach WorkItem."
    if intent == KBA_INTENT_MORE_RESEARCH:
        return "Run deeper source-backed business research for this WorkItem."
    if intent == KBA_INTENT_RUN_AGAIN:
        return "Run this WorkItem route again with the same request context."
    return "Advance this WorkItem from a Slack business-agent action."


def _record_kba_action_event(
    store: SQLiteStore,
    work_item: Any,
    action_payload: BusinessAgentActionPayload,
    *,
    action_id: str,
    reviewer: str,
    dedupe_key: str,
    summary: str,
) -> None:
    record_event(
        work_item,
        event_type="slack_action_intent",
        actor=reviewer or "slack",
        summary=summary,
        metadata={
            "schema": action_payload.schema_name,
            "intent": action_payload.intent,
            "action_id": action_id,
            "approval_id": action_payload.approval_id,
            "gate_scope": action_payload.gate_scope,
            "artifact_id": action_payload.artifact_id,
            "dedupe_key": dedupe_key,
            "slack": {
                "channel_id": action_payload.source_channel_id,
                "message_ts": action_payload.source_message_ts,
                "thread_ts": action_payload.source_thread_ts,
            },
        },
        store=store,
    )


def _has_recorded_kba_action(store: SQLiteStore, work_item_id: str, dedupe_key: str) -> bool:
    if not dedupe_key:
        return False
    return any(
        event.event_type == "slack_action_intent"
        and event.metadata.get("dedupe_key") == dedupe_key
        for event in store.list_work_item_events(work_item_id)
    )


def _kba_dedupe_key(action_id: str, action_payload: BusinessAgentActionPayload) -> str:
    return ":".join(
        [
            action_id,
            action_payload.intent,
            action_payload.approval_id,
            action_payload.source_channel_id,
            action_payload.source_message_ts,
        ]
    )


def _sources_payload(item: Any | None, work_item: Any | None) -> dict[str, Any]:
    metadata = (item.metadata or {}) if item is not None else {}
    item_sources = metadata.get("sources") if isinstance(metadata.get("sources"), list) else []
    work_item_sources = []
    if work_item is not None:
        work_item_sources = [source.model_dump(mode="json") for source in work_item.sources[:8]]
    return {
        "approval_id": item.id if item is not None else "",
        "work_item_id": work_item.id if work_item is not None else "",
        "approval_sources": item_sources[:8],
        "work_item_sources": work_item_sources,
    }


def _sources_followup_text(read_only: dict[str, Any]) -> str:
    approval_count = len(read_only.get("approval_sources") or [])
    work_item_count = len(read_only.get("work_item_sources") or [])
    total = approval_count + work_item_count
    if total:
        return f"Sources are available in-thread ({total} source reference(s))."
    return "No source references are attached to this approval card yet."


def _kba_result(
    action_payload: BusinessAgentActionPayload,
    *,
    action_id: str,
    approval_status: ApprovalQueueStatus,
    reviewer: str,
    outcome: str,
    followup_text: str,
    item: Any | None,
    work_item: Any | None,
    read_only_payload: dict[str, Any] | None = None,
    queued_route: str = "",
    queued_command: str = "",
    idempotent: bool = False,
) -> SlackApprovalInteractionResult:
    return SlackApprovalInteractionResult(
        approval_id=action_payload.approval_id,
        action_id=action_id,
        approval_status=approval_status,
        stage="work_item" if queued_route else "action",
        object_type=item.object_type if item is not None else "",
        object_id=item.object_id if item is not None else "",
        source_agent=item.source_agent if item is not None else "",
        reviewer=reviewer,
        outcome=outcome,
        followup_text=followup_text,
        queued_route=queued_route,
        queued_command=queued_command,
        read_only_payload=read_only_payload,
        work_item_id=work_item.id if work_item is not None else action_payload.work_item_id,
        work_item_status=work_item.status.value if work_item is not None else "",
        idempotent=idempotent,
        slack_channel_id=action_payload.source_channel_id,
        slack_message_ts=action_payload.source_message_ts,
    )


def _queued_command(work_item_id: str, route: str) -> str:
    return f"workitem continue {work_item_id} --route {route}"


def _modal_update_response(*, title: str, text: str) -> dict[str, Any]:
    return {
        "response_action": "update",
        "view": {
            "type": "modal",
            "title": {"type": "plain_text", "text": title[:24] or "Keystone"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text[:2900] or "Action complete."},
                }
            ],
        },
    }


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


def _linked_work_item(store: SQLiteStore, item: Any) -> Any | None:
    metadata = item.metadata or {}
    work_item_id = str(metadata.get("work_item_id") or "").strip()
    if not work_item_id:
        return None
    work_item = store.get_work_item(work_item_id)
    if work_item is None:
        raise KeyError(f"WorkItem not found for approval {item.id}: {work_item_id}")
    if not any(gate.approval_id == item.id for gate in work_item.approval_gates):
        raise ValueError(f"stale WorkItem approval id: {item.id}")
    return work_item


def _slack_context(payload: dict[str, Any]) -> dict[str, str]:
    channel = payload.get("channel")
    container = payload.get("container")
    message = payload.get("message")
    team = payload.get("team")
    message_ts = str(
        (
            container.get("message_ts")
            if isinstance(container, dict)
            else ""
        )
        or (message.get("ts") if isinstance(message, dict) else "")
        or ""
    )
    return {
        "team_id": str(team.get("id") if isinstance(team, dict) else payload.get("team_id") or ""),
        "channel_id": str(channel.get("id") if isinstance(channel, dict) else ""),
        "channel_name": str(channel.get("name") if isinstance(channel, dict) else ""),
        "message_ts": message_ts,
        "thread_ts": str(
            (
                container.get("thread_ts")
                if isinstance(container, dict)
                else ""
            )
            or (message.get("thread_ts") if isinstance(message, dict) else "")
            or message_ts
            or ""
        ),
        "response_url_present": str(bool(payload.get("response_url"))).lower(),
    }


def _work_item_followup_text(
    status: ApprovalQueueStatus,
    gate_update: Any,
    *,
    feedback: str,
) -> str:
    if not gate_update.changed:
        return (
            "Duplicate Slack action ignored. The WorkItem approval gate already has "
            f"state `{gate_update.new_state}`."
        )
    if status == ApprovalQueueStatus.APPROVED:
        return (
            f"Approved. WorkItem `{gate_update.work_item.id}` gate "
            f"`{gate_update.gate_scope}` is now `{gate_update.new_state}`. "
            "No email, Slack post, publication, schedule, or live draft was created."
        )
    if status == ApprovalQueueStatus.REVISE:
        suffix = (
            " Feedback was captured."
            if feedback
            else " Add edit instructions before re-drafting."
        )
        return (
            f"Revision requested. WorkItem `{gate_update.work_item.id}` remains blocked."
            f"{suffix}"
        )
    if status == ApprovalQueueStatus.REJECTED:
        suffix = " Feedback was captured." if feedback else " Add a rejection reason for the agent."
        return f"Rejected. WorkItem `{gate_update.work_item.id}` remains blocked.{suffix}"
    return f"WorkItem `{gate_update.work_item.id}` approval gate updated."


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
        return (
            "Approved for the recorded scope. Use the explicit Gmail draft command if a "
            "draft should be created; no email was sent."
        )
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


def _slack_draft_creation_allowed(item: Any) -> bool:
    metadata = item.metadata or {}
    return (
        _is_email_outreach_item(item)
        and bool(metadata.get("slack_approval_allows_gmail_draft_creation"))
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
    target_account = _target_gmail_draft_account(metadata)
    subject, body = _parse_email_draft_text(item.draft_text or "")
    subject = str(metadata.get("email_subject") or subject).strip()
    if not recipient:
        return {
            "status": "skipped",
            "reason": "missing confirmed recipient email",
            "gmail_account": target_account,
            "send_enabled": False,
        }
    if not subject:
        return {
            "status": "skipped",
            "reason": "missing email subject",
            "gmail_account": target_account,
            "send_enabled": False,
        }
    if not body:
        return {
            "status": "skipped",
            "reason": "missing email body",
            "gmail_account": target_account,
            "send_enabled": False,
        }
    return GmailTool(live=live_gmail).create_draft(
        to=recipient,
        subject=subject,
        body=body,
        expected_account=target_account,
    )


def _duplicate_gmail_draft_result(item: Any) -> dict[str, Any]:
    metadata = item.metadata or {}
    existing = metadata.get("gmail_draft_result")
    result: dict[str, Any] = {
        "status": "duplicate_ignored",
        "reason": "approval was already recorded; no additional Gmail draft was created",
        "gmail_account": _target_gmail_draft_account(metadata),
        "send_enabled": False,
    }
    if isinstance(existing, dict):
        result["existing_status"] = str(existing.get("status") or "")
        result["existing_draft_id"] = str(existing.get("draft_id") or "")
    return result


def _target_gmail_draft_account(metadata: dict[str, Any]) -> str:
    for key in ("gmail_draft_account", "target_gmail_account"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    for key in GMAIL_DRAFT_ACCOUNT_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_GMAIL_DRAFT_ACCOUNT


def _gmail_draft_account_text(result: dict[str, Any]) -> str:
    account = str(result.get("gmail_account") or "").strip()
    return f" in `{account}`" if account else ""


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
