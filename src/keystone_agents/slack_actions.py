"""Slack message-action helpers for starting Keystone WorkItem runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.automation_inventory import build_automation_inventory_report
from keystone_agents.eval_runtime_diagnostics import slack_eval_blocker_diagnostics
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.sdk_sessions import build_sdk_session, resolve_sdk_session_spec
from keystone_agents.slack_action_contract import (
    KBA_EVAL_ORCHESTRATOR_JUDGE,
    KBA_EVAL_REVIEW,
    KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
    KBA_INTENT_EVAL_REVIEW,
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    RUN_AGENT_TASK_ACTION_ID,
    RUN_AGENT_TASK_BLOCK_ID,
    RUN_AGENT_VIEW_CALLBACK_ID,
    SLACK_SELECTED_CONTEXT_SCHEMA,
    business_agent_action_value,
    slack_agent_feedback_event,
)
from keystone_agents.slack_interactions import parse_slack_interaction_payload
from keystone_agents.slack_query_prompts import (
    build_slack_query_prompt_input,
    resolve_slack_query_prompt,
    slack_query_prompt_external_context,
)
from keystone_agents.workflow_runner import advance_work_item_manager_loop

DEFAULT_SLACK_CONTEXT_DIR = Path("artifacts/slack_contexts")

_MAX_MESSAGE_TEXT_CHARS = 4000
_MAX_THREAD_MESSAGES = 20
_MAX_SLACK_CONTEXT_WINDOW_DAYS = 7
_MAX_SLACK_CONTEXT_WINDOW_SECONDS = _MAX_SLACK_CONTEXT_WINDOW_DAYS * 24 * 60 * 60
_MAX_PRIVATE_METADATA_CHARS = 2800
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EVAL_CASE_ID_RE = re.compile(
    r"\b(?:eval\s+case|case(?:_id)?)\s*(?:[:=]\s*|\s+)([A-Za-z0-9_.:-]+)",
    re.IGNORECASE,
)


class SlackContextMessage(BaseModel):
    """One bounded Slack message included in selected context."""

    ts: str = ""
    user_id: str = ""
    username: str = ""
    text: str = ""
    permalink: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SlackSelectedMessageContext(BaseModel):
    """Local-only context captured from a selected Slack message or thread."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: Literal["keystone.slack.selected_message_context.v1"] = Field(
        default=SLACK_SELECTED_CONTEXT_SCHEMA,
        alias="schema",
    )
    source: str = "slack_message_action"
    team_id: str = ""
    team_domain: str = ""
    channel_id: str = ""
    channel_name: str = ""
    selected_message_ts: str = ""
    thread_ts: str = ""
    permalink: str = ""
    selected_message: SlackContextMessage = Field(default_factory=SlackContextMessage)
    thread_messages: list[SlackContextMessage] = Field(default_factory=list)
    prior_agent_runs: list[dict[str, Any]] = Field(default_factory=list)
    thread_fetch_status: Literal["ok", "failed", "not_requested"] = "not_requested"
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    eval_metadata: dict[str, Any] = Field(default_factory=dict, alias="eval")


class SlackAgentRunSubmission(BaseModel):
    """Parsed modal submission for a selected Slack context run."""

    requested_task: str
    context_file_path: str = ""
    context: SlackSelectedMessageContext | None = None
    user_id: str = ""
    user_name: str = ""


class SlackAgentActionResult(BaseModel):
    """Result returned to a Slack runtime bridge."""

    stage: Literal["modal", "work_item"]
    callback_id: str
    modal_view: dict[str, Any] | None = None
    context_file_path: str = ""
    work_item: dict[str, Any] | None = None
    route: str = ""
    status: str = ""
    warnings: list[str] = Field(default_factory=list)
    feedback_events: list[dict[str, Any]] = Field(default_factory=list)
    run_provenance: dict[str, Any] = Field(default_factory=dict)
    eval_record: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


def slack_message_action_manifest_patch() -> dict[str, Any]:
    """Return the Slack manifest fragment needed for the message shortcut."""

    return {
        "features": {
            "shortcuts": [
                {
                    "name": "Run Keystone Agent",
                    "type": "message",
                    "callback_id": RUN_AGENT_MESSAGE_CALLBACK_ID,
                    "description": "Run a Keystone business agent using this message as context.",
                }
            ]
        },
        "settings": {"interactivity": {"is_enabled": True}},
    }


def is_run_agent_interaction_payload(raw_payload: str | dict[str, Any]) -> bool:
    """Return whether a Slack payload belongs to the Keystone run-agent flow."""

    payload = parse_slack_interaction_payload(raw_payload)
    if str(payload.get("type") or "").strip() == "message_action":
        return str(payload.get("callback_id") or "").strip() == RUN_AGENT_MESSAGE_CALLBACK_ID
    if str(payload.get("type") or "").strip() == "view_submission":
        view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
        return str(view.get("callback_id") or "").strip() == RUN_AGENT_VIEW_CALLBACK_ID
    return False


def build_selected_message_context(
    raw_payload: str | dict[str, Any],
    *,
    thread_messages: list[dict[str, Any]] | None = None,
    thread_fetch_error: str = "",
) -> SlackSelectedMessageContext:
    """Convert a Slack message shortcut payload into bounded local context."""

    payload = parse_slack_interaction_payload(raw_payload)
    if str(payload.get("type") or "").strip() != "message_action":
        raise ValueError("Slack run-agent context requires a message_action payload.")
    if str(payload.get("callback_id") or "").strip() != RUN_AGENT_MESSAGE_CALLBACK_ID:
        raise ValueError("Unsupported Slack message action callback id.")

    team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
    channel = payload.get("channel") if isinstance(payload.get("channel"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    channel_id = _clean_scalar(channel.get("id") or payload.get("channel_id"))
    selected_ts = _clean_scalar(
        message.get("ts")
        or payload.get("message_ts")
        or _nested(payload, "container", "message_ts")
    )
    thread_ts = _clean_scalar(
        message.get("thread_ts") or _nested(payload, "container", "thread_ts") or selected_ts
    )
    permalink = _clean_scalar(
        message.get("permalink")
        or payload.get("message_permalink")
        or _slack_permalink(channel_id, selected_ts)
    )
    selected = _message_from_payload(message, fallback_ts=selected_ts, fallback_permalink=permalink)
    provided_thread_messages = thread_messages
    if provided_thread_messages is None and isinstance(payload.get("thread_messages"), list):
        provided_thread_messages = payload["thread_messages"]
    bounded_thread, dropped_old_messages = _ordered_thread_messages_with_policy(
        provided_thread_messages or []
    )
    warnings: list[str] = []
    if dropped_old_messages:
        warnings.append(
            "Slack thread context was limited to the selected message plus the most "
            f"recent {_MAX_SLACK_CONTEXT_WINDOW_DAYS} days to keep agent context bounded."
        )
    if thread_fetch_error:
        warnings.append(
            "Slack thread fetch failed: "
            f"{_clean_text(thread_fetch_error, max_chars=240)}. "
            "Proceeding with selected message metadata only."
        )
    fetch_status: Literal["ok", "failed", "not_requested"]
    if thread_fetch_error:
        fetch_status = "failed"
    elif bounded_thread:
        fetch_status = "ok"
    else:
        fetch_status = "not_requested"

    return SlackSelectedMessageContext(
        team_id=_clean_scalar(team.get("id") or payload.get("team_id")),
        team_domain=_clean_scalar(team.get("domain") or payload.get("team_domain")),
        channel_id=channel_id,
        channel_name=_clean_scalar(channel.get("name") or payload.get("channel_name")),
        selected_message_ts=selected_ts,
        thread_ts=thread_ts,
        permalink=permalink,
        selected_message=selected,
        thread_messages=bounded_thread,
        thread_fetch_status=fetch_status,
        warnings=warnings,
        metadata={
            "trigger_id": _clean_scalar(payload.get("trigger_id")),
            "response_url_present": bool(payload.get("response_url")),
            "context_scope": "selected_message_or_thread_recent_window",
            "context_window_days": _MAX_SLACK_CONTEXT_WINDOW_DAYS,
            "max_thread_messages": _MAX_THREAD_MESSAGES,
            "channel_history_included": False,
        },
        eval=_eval_metadata_from_payload(payload, message),
    )


def write_selected_message_context_file(
    context: SlackSelectedMessageContext,
    *,
    directory: str | Path = DEFAULT_SLACK_CONTEXT_DIR,
) -> Path:
    """Write selected Slack context to a local JSON file and return the path."""

    context_dir = Path(directory).expanduser().resolve()
    context_dir.mkdir(parents=True, exist_ok=True)
    digest_source = "|".join(
        [
            context.team_id,
            context.channel_id,
            context.selected_message_ts,
            context.thread_ts,
            context.selected_message.text,
        ]
    )
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:12]
    safe_ts = re.sub(r"[^0-9A-Za-z_-]+", "-", context.selected_message_ts or "message")
    filename = f"slack-context-{safe_ts}-{digest}.json"
    path = (context_dir / filename).resolve()
    payload = context.model_dump(mode="json", by_alias=True)
    metadata = payload.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata.setdefault("sensitivity", "slack_context_local_only")
        metadata.setdefault("retention", "operator_review_artifact_cleanup_when_no_longer_needed")
        metadata.setdefault("context_scope", "selected_message_or_thread_recent_window")
        metadata.setdefault("raw_text_persistence", "bounded_operator_selected_context")
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def load_selected_message_context_file(path: str | Path) -> SlackSelectedMessageContext:
    """Read a selected Slack context file."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SlackSelectedMessageContext.model_validate(data)


def build_run_agent_modal(
    context: SlackSelectedMessageContext,
    *,
    context_file_path: str | Path = "",
) -> dict[str, Any]:
    """Build the Slack modal view that captures the user's task."""

    private_metadata = _run_agent_modal_private_metadata(
        context,
        context_file_path=context_file_path,
    )
    if len(private_metadata) > _MAX_PRIVATE_METADATA_CHARS:
        raise ValueError("Slack run-agent private metadata exceeds safe size limit.")
    metadata = {
        "type": "modal",
        "callback_id": RUN_AGENT_VIEW_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Run Keystone Agent"},
        "submit": {"type": "plain_text", "text": "Run"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private_metadata,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _modal_context_summary(context),
                },
            },
            {
                "type": "input",
                "block_id": RUN_AGENT_TASK_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Task"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": RUN_AGENT_TASK_ACTION_ID,
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Example: research this company and summarize next steps",
                    },
                },
            },
        ],
    }
    return metadata


def _run_agent_modal_private_metadata(
    context: SlackSelectedMessageContext,
    *,
    context_file_path: str | Path = "",
) -> str:
    metadata = {
        "schema": context.schema_,
        "context_file_path": str(context_file_path) if context_file_path else "",
        "selected_context": _modal_embedded_context(context),
        "channel_id": context.channel_id,
        "selected_message_ts": context.selected_message_ts,
        "thread_ts": context.thread_ts,
        "permalink": context.permalink,
        "warnings": context.warnings,
        "eval": context.eval_metadata,
    }
    private_metadata = json.dumps(metadata, ensure_ascii=True, sort_keys=True)
    if len(private_metadata) <= _MAX_PRIVATE_METADATA_CHARS:
        return private_metadata

    metadata = {
        "schema": context.schema_,
        "context_file_path": str(context_file_path) if context_file_path else "",
        "selected_context": _modal_embedded_context(context, include_text=False),
        "channel_id": context.channel_id,
        "selected_message_ts": context.selected_message_ts,
        "thread_ts": context.thread_ts,
        "permalink": "",
        "warnings": [],
        "eval": context.eval_metadata,
    }
    private_metadata = json.dumps(metadata, ensure_ascii=True, sort_keys=True)
    if len(private_metadata) <= _MAX_PRIVATE_METADATA_CHARS:
        return private_metadata

    metadata = _modal_pointer_metadata(context, context_file_path=context_file_path)
    return json.dumps(metadata, ensure_ascii=True, sort_keys=True)


def _modal_pointer_metadata(
    context: SlackSelectedMessageContext,
    *,
    context_file_path: str | Path = "",
) -> dict[str, Any]:
    """Return the smallest parseable private_metadata payload."""

    selected_context = {
        "schema": context.schema_,
        "source": context.source,
        "team_id": _metadata_scalar(context.team_id),
        "team_domain": _metadata_scalar(context.team_domain),
        "channel_id": _metadata_scalar(context.channel_id),
        "channel_name": _metadata_scalar(context.channel_name),
        "selected_message_ts": _metadata_scalar(context.selected_message_ts),
        "thread_ts": _metadata_scalar(context.thread_ts),
        "permalink": "",
        "selected_message": {
            "ts": _metadata_scalar(context.selected_message.ts),
            "user_id": _metadata_scalar(context.selected_message.user_id),
            "username": _metadata_scalar(context.selected_message.username),
            "text": "",
            "permalink": "",
        },
        "thread_fetch_status": context.thread_fetch_status,
        "warnings": [],
        "metadata": {"fallback_scope": "selected_message_pointer"},
        "eval": context.eval_metadata,
    }
    return {
        "schema": context.schema_,
        "context_file_path": _clean_text(context_file_path, max_chars=1800),
        "selected_context": selected_context,
        "channel_id": _metadata_scalar(context.channel_id),
        "selected_message_ts": _metadata_scalar(context.selected_message_ts),
        "thread_ts": _metadata_scalar(context.thread_ts),
        "warnings": ["Slack modal metadata reduced to context pointer."],
        "eval": context.eval_metadata,
    }


def _modal_embedded_context(
    context: SlackSelectedMessageContext,
    *,
    include_text: bool = True,
) -> dict[str, Any]:
    """Return a deliberately tiny fallback context for Slack private_metadata."""

    selected_text = (
        _clean_text(context.selected_message.text, max_chars=500) if include_text else ""
    )
    return {
        "schema": context.schema_,
        "source": context.source,
        "team_id": context.team_id,
        "team_domain": context.team_domain,
        "channel_id": context.channel_id,
        "channel_name": context.channel_name,
        "selected_message_ts": context.selected_message_ts,
        "thread_ts": context.thread_ts,
        "permalink": _clean_scalar(context.permalink) if include_text else "",
        "selected_message": {
            "ts": context.selected_message.ts,
            "user_id": context.selected_message.user_id,
            "username": context.selected_message.username,
            "text": selected_text,
            "permalink": _clean_scalar(context.selected_message.permalink) if include_text else "",
        },
        "thread_fetch_status": context.thread_fetch_status,
        "warnings": [_clean_text(item, max_chars=180) for item in context.warnings[:2]],
        "metadata": {"fallback_scope": "selected_message_minimal"},
        "eval": context.eval_metadata,
    }


def parse_run_agent_modal_submission(
    raw_payload: str | dict[str, Any],
    *,
    context: SlackSelectedMessageContext | None = None,
) -> SlackAgentRunSubmission:
    """Parse the modal submission and recover the selected context pointer."""

    payload = parse_slack_interaction_payload(raw_payload)
    if str(payload.get("type") or "").strip() != "view_submission":
        raise ValueError("Slack run-agent submission requires a view_submission payload.")
    view = payload.get("view") if isinstance(payload.get("view"), dict) else {}
    if str(view.get("callback_id") or "").strip() != RUN_AGENT_VIEW_CALLBACK_ID:
        raise ValueError("Unsupported Slack modal callback id.")
    task = _first_plain_text_input(view)
    if not task:
        raise ValueError("A Keystone agent task is required.")
    private_metadata = _json_object(view.get("private_metadata"))
    metadata_context = private_metadata.get("selected_context")
    selected_context = context
    if selected_context is None and isinstance(metadata_context, dict):
        selected_context = SlackSelectedMessageContext.model_validate(metadata_context)
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    return SlackAgentRunSubmission(
        requested_task=task,
        context_file_path=_clean_scalar(private_metadata.get("context_file_path")),
        context=selected_context,
        user_id=_clean_scalar(user.get("id")),
        user_name=_clean_scalar(user.get("username") or user.get("name")),
    )


def handle_run_agent_interaction(
    raw_payload: str | dict[str, Any],
    *,
    database_url: str | None = None,
    context_dir: str | Path = DEFAULT_SLACK_CONTEXT_DIR,
    live_search: bool = False,
    live_sdk: bool = False,
    max_results: int = 3,
    thread_messages: list[dict[str, Any]] | None = None,
    thread_fetch_error: str = "",
    feedback_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> SlackAgentActionResult:
    """Handle a Slack run-agent message action or modal submission locally."""

    payload = parse_slack_interaction_payload(raw_payload)
    payload_type = str(payload.get("type") or "").strip()
    if payload_type == "message_action":
        context = build_selected_message_context(
            payload,
            thread_messages=thread_messages,
            thread_fetch_error=thread_fetch_error,
        )
        context_path = write_selected_message_context_file(context, directory=context_dir)
        return SlackAgentActionResult(
            stage="modal",
            callback_id=RUN_AGENT_MESSAGE_CALLBACK_ID,
            modal_view=build_run_agent_modal(context, context_file_path=context_path),
            context_file_path=str(context_path),
            warnings=context.warnings,
        )
    if payload_type == "view_submission":
        submission = parse_run_agent_modal_submission(payload)
        context_file_path = submission.context_file_path
        if not context_file_path and submission.context is not None:
            context_file_path = str(
                write_selected_message_context_file(submission.context, directory=context_dir)
            )
        context_warnings: list[str] = []
        selected_context = submission.context
        if context_file_path:
            resolved_context_path = _resolve_context_file_path(
                context_file_path,
                context_dir=context_dir,
            )
            if resolved_context_path.is_file():
                selected_context = load_selected_message_context_file(resolved_context_path)
                context_file_path = str(resolved_context_path)
            elif selected_context is not None:
                selected_context.warnings.append(
                    "Slack context file was unavailable; used embedded modal context fallback."
                )
                context_file_path = str(
                    write_selected_message_context_file(selected_context, directory=context_dir)
                )
            else:
                context_warnings.append(
                    "Slack context file was unavailable and no embedded fallback was present."
                )
                context_file_path = ""
        if selected_context is not None:
            context_warnings = selected_context.warnings
        feedback_events: list[dict[str, Any]] = []
        slack_feedback_callback = _build_feedback_collector(
            feedback_events,
            upstream_callback=feedback_callback,
        )
        preflight_session = _slack_context_sdk_session(selected_context, enabled=live_sdk)
        deterministic_plan = infer_manual_request_plan(
            submission.requested_task,
            source="slack_modal_preflight_hint",
        )
        orchestrator_preflight = run_orchestrator_preflight(
            submission.requested_task,
            requested_agent=deterministic_plan.requested_agent,
            live_manual_plan=live_sdk,
            session=preflight_session,
            database_url=database_url,
            workflow_state=_orchestrator_workflow_state_from_slack_context(
                selected_context,
                request_text=submission.requested_task,
                database_url=database_url,
            ),
        )
        slack_feedback_callback(
            "orchestrator_preflight",
            {
                "request_text": orchestrator_preflight.request_text,
                "requested_agent": orchestrator_preflight.requested_agent,
                "selected_agent": orchestrator_preflight.selected_agent,
                "advisory_only": orchestrator_preflight.advisory_only,
                "blocked_by_orchestrator": orchestrator_preflight.blocked_by_orchestrator,
                "execution_allowed": orchestrator_preflight.execution_allowed,
                "block_kind": orchestrator_preflight.block_kind,
                "block_reason": orchestrator_preflight.block_reason,
                "route": orchestrator_preflight.route_result.route,
                "refused": orchestrator_preflight.route_result.refused,
                "stop_reason": orchestrator_preflight.route_result.stop_reason,
            },
        )
        if not orchestrator_preflight.execution_allowed:
            return _blocked_slack_agent_result(
                submission=submission,
                context=selected_context,
                context_file_path=context_file_path,
                context_warnings=context_warnings,
                feedback_events=feedback_events,
                orchestrator_preflight=orchestrator_preflight,
            )
        manual_request_plan = orchestrator_preflight.manual_request_plan.model_dump(mode="json")
        slack_query_prompt = _slack_query_prompt_for_submission(
            submission,
            selected_context,
            manual_request_plan=manual_request_plan,
        )
        if slack_query_prompt is not None:
            slack_feedback_callback(
                "slack_query_prompt_selected",
                slack_query_prompt.metadata(),
            )
        request_options = _slack_cost_conservation_request_options(submission.requested_task)
        if (
            slack_query_prompt is not None
            and slack_query_prompt.cost_profile != "standard"
            and "cost_profile" not in request_options
        ):
            request_options["cost_profile"] = slack_query_prompt.cost_profile
        result = advance_work_item_manager_loop(
            WorkflowRunRequest(
                request_text=submission.requested_task,
                save=True,
                database_url=database_url,
                live_search=live_search,
                live_sdk=live_sdk,
                max_results=max_results,
                context_file_path=context_file_path,
                manual_request_plan=manual_request_plan,
                orchestrator_preflight=compact_orchestrator_preflight_payload(
                    orchestrator_preflight
                ),
                external_context=slack_query_prompt_external_context(slack_query_prompt),
                slack_query_prompt=(
                    slack_query_prompt.model_dump(mode="json", by_alias=True)
                    if slack_query_prompt is not None
                    else None
                ),
                **request_options,
            ),
            feedback_callback=slack_feedback_callback,
        )
        run_provenance = _slack_run_provenance(
            submission=submission,
            context=selected_context,
            context_file_path=context_file_path,
            result=result,
        )
        if run_provenance["validation_errors"]:
            raise ValueError(
                "Slack run provenance mismatch: " + "; ".join(run_provenance["validation_errors"])
            )
        result_payload = result.model_dump(mode="json")
        result_payload["slack_run_provenance"] = run_provenance
        eval_record = _record_eval_run_for_slack_bridge(
            submission.requested_task,
            context=selected_context,
            result=result,
            run_provenance=run_provenance,
        )
        if eval_record is not None:
            eval_actions = _eval_slack_actions(eval_record)
            result_payload["eval_record"] = eval_record
            result_payload["slack_actions"] = eval_actions
            result_payload["slack_overflow_actions"] = []
            result_payload["human_summary"] = _append_eval_thread_guidance(
                str(result_payload.get("human_summary") or ""),
                eval_record=eval_record,
            )
        return SlackAgentActionResult(
            stage="work_item",
            callback_id=RUN_AGENT_VIEW_CALLBACK_ID,
            context_file_path=context_file_path,
            work_item=result.work_item.model_dump(mode="json"),
            route=result.route.value,
            status=result.status.value,
            warnings=context_warnings,
            feedback_events=feedback_events,
            run_provenance=run_provenance,
            eval_record=eval_record,
            result=result_payload,
        )
    raise ValueError(f"Unsupported Slack run-agent payload type: {payload_type or 'unknown'}")


def _slack_cost_conservation_request_options(request_text: str) -> dict[str, Any]:
    """Let the WorkItem runner choose route-aware Slack cost controls."""

    return {}


def _record_eval_run_for_slack_bridge(
    request_text: str,
    *,
    context: SlackSelectedMessageContext | None,
    result: Any,
    run_provenance: dict[str, Any],
) -> dict[str, Any] | None:
    if context is None:
        return None
    try:
        from promptfoo.eval_database import (
            DEFAULT_EVAL_DB,
            record_slack_eval_run,
            resolve_slack_eval_case_id,
        )
    except ImportError:
        return None

    database_path = Path(os.environ.get("KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB") or DEFAULT_EVAL_DB)
    agent = str(getattr(getattr(result, "route", ""), "value", getattr(result, "route", "")))
    case_id = _clean_scalar(context.eval_metadata.get("case_id"))
    if not case_id:
        case_id = _extract_eval_case_id_from_request(request_text)
    if not case_id:
        try:
            case_id = resolve_slack_eval_case_id(
                request_text=request_text,
                agent=agent,
                slack_channel_id=context.channel_id,
                slack_channel_name=context.channel_name,
                database_path=database_path,
            )
        except (OSError, ValueError):
            case_id = ""
    if not case_id:
        return None

    run_id = str(run_provenance.get("work_item_id") or "").strip()
    evidence = _slack_eval_evidence(
        context=context,
        result=result,
        run_provenance=run_provenance,
    )
    try:
        row_id = record_slack_eval_run(
            case_id=case_id,
            run_id=run_id,
            agent=agent,
            work_item_id=run_id,
            slack_channel_id=context.channel_id,
            slack_channel_name=context.channel_name,
            slack_thread_ts=context.thread_ts or context.selected_message_ts,
            permalink=context.permalink,
            request_text=request_text,
            result_summary=str(getattr(result, "human_summary", "") or ""),
            route=str(evidence.get("route") or agent),
            status=str(evidence.get("status") or ""),
            context_policy=str(evidence.get("context_policy") or ""),
            thread_fetch_status=str(evidence.get("thread_fetch_status") or ""),
            thread_message_count=int(evidence.get("thread_message_count") or 0),
            warning_count=int(evidence.get("warning_count") or 0),
            warnings=[str(item) for item in evidence.get("warnings") or []],
            cost_profile=str(evidence.get("cost_profile") or ""),
            source_count=int(evidence.get("source_count") or 0),
            visible_source_count=int(evidence.get("visible_source_count") or 0),
            sdk_estimated_cost_usd=evidence.get("sdk_estimated_cost_usd"),
            sdk_cache_hit_rate=evidence.get("sdk_cache_hit_rate"),
            duration_ms=evidence.get("duration_ms"),
            response_hash=str(evidence.get("response_hash") or ""),
            evidence=evidence,
            prompt_versions=[
                item for item in evidence.get("prompt_versions") or [] if isinstance(item, dict)
            ],
            prompt_metadata=(
                evidence.get("prompt_metadata")
                if isinstance(evidence.get("prompt_metadata"), dict)
                else {}
            ),
            model_provider=str(evidence.get("model_provider") or ""),
            model_name=str(evidence.get("model_name") or ""),
            run_mode=str(evidence.get("run_mode") or ""),
            search_provider=str(evidence.get("search_provider") or ""),
            search_provider_sequence=[
                str(item) for item in evidence.get("search_provider_sequence") or []
            ],
            database_path=database_path,
        )
    except (OSError, ValueError):
        return None

    try:
        from promptfoo.eval_dashboard import slack_run_post_save_state

        post_save_state = slack_run_post_save_state(
            case_id=case_id,
            row_id=row_id,
            database_path=database_path,
        )
    except (ImportError, OSError, ValueError, sqlite3.Error):
        post_save_state = {}
    dashboard = _render_eval_dashboard(database_path)
    dashboard_case_url = _eval_dashboard_case_url(case_id)
    review_case_url = _eval_review_case_url(case_id)
    eval_actions = _eval_slack_actions(
        {
            "case_id": case_id,
            "run_id": run_id,
            "agent": agent,
            "slack_thread_ts": context.thread_ts or context.selected_message_ts,
            "dashboard_case_url": dashboard_case_url,
            "review_case_url": review_case_url,
        }
    )
    return {
        "id": row_id,
        "case_id": case_id,
        "run_id": run_id,
        "agent": agent,
        "database_path": str(database_path),
        "slack_thread_ts": context.thread_ts or context.selected_message_ts,
        "dashboard_url": _eval_dashboard_url(),
        "dashboard_case_url": dashboard_case_url,
        "review_case_url": review_case_url,
        "dashboard_path": dashboard.get("dashboard_path", ""),
        "dashboard_relative_path": dashboard.get("dashboard_relative_path", ""),
        "evidence": evidence,
        "post_save_state": post_save_state,
        "dashboard_visibility": post_save_state.get("dashboard_visibility", {}),
        "refresh_endpoints": post_save_state.get("refresh_endpoints", []),
        "scorecard_request": "@KNI can you give me a scorecard for this eval?",
        "orchestrator_judge_action": "Score with Orchestrator Judge",
        "orchestrator_judge_effect": (
            "When enabled, fills the same backend review form for the saved #evals Slack output "
            "and refreshes dashboard scoring/database/analysis."
        ),
        "submit_evaluation_action": "Submit Evaluation",
        "slack_actions": eval_actions,
        "eval_thread_reply": {
            "case_id": case_id,
            "run_id": run_id,
            "dashboard_case_url": dashboard_case_url,
            "review_case_url": review_case_url,
            "scorecard_request": "@KNI can you give me a scorecard for this eval?",
            "orchestrator_judge_action": "Score with Orchestrator Judge",
            "submit_evaluation_action": "Submit Evaluation",
            "submit_evaluation_effect": "Writes scores and human notes to the local eval database, then refreshes dashboard views.",
            "slack_actions": eval_actions,
        },
    }


def _extract_eval_case_id_from_request(request_text: str) -> str:
    compact = " ".join(str(request_text or "").split())
    lowered = compact.lower()
    if "eval" not in lowered and "case" not in lowered:
        return ""
    match = _EVAL_CASE_ID_RE.search(compact)
    return _clean_scalar(match.group(1)) if match else ""


def _slack_eval_evidence(
    *,
    context: SlackSelectedMessageContext,
    result: Any,
    run_provenance: dict[str, Any],
) -> dict[str, Any]:
    work_item = getattr(result, "work_item", None)
    work_item_payload = (
        work_item.model_dump(mode="json") if hasattr(work_item, "model_dump") else {}
    )
    target = work_item_payload.get("target") if isinstance(work_item_payload.get("target"), dict) else {}
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    slack_context = (
        metadata.get("slack_context") if isinstance(metadata.get("slack_context"), dict) else {}
    )
    query_prompt = (
        slack_context.get("query_prompt")
        if isinstance(slack_context.get("query_prompt"), dict)
        else {}
    )
    sources = work_item_payload.get("sources") if isinstance(work_item_payload.get("sources"), list) else []
    warnings = [str(item) for item in slack_context.get("warnings") or context.warnings or []]
    human_summary = str(getattr(result, "human_summary", "") or "")
    sdk_usage = _latest_sdk_usage_from_result(result)
    sdk_cost = _latest_sdk_cost_from_result(result)
    result_payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    model = result_payload.get("model") if isinstance(result_payload.get("model"), dict) else {}
    retrieval = (
        result_payload.get("retrieval")
        if isinstance(result_payload.get("retrieval"), dict)
        else {}
    )
    duration_ms = _number_or_none(
        result_payload.get("duration_ms")
        or result_payload.get("elapsed_ms")
        or result_payload.get("runtime_ms")
    )
    thread_messages = slack_context.get("thread_messages") or context.thread_messages or []
    selected_message_count = 1 if (context.selected_message.ts or context.selected_message.text) else 0
    thread_message_count = len(thread_messages) if thread_messages else selected_message_count
    source_channel_id = context.channel_id or str(run_provenance.get("source_channel_id") or "")
    source_channel_name = context.channel_name or ""
    source_thread_ts = (
        context.thread_ts
        or context.selected_message_ts
        or str(run_provenance.get("source_thread_ts") or "")
    )
    evidence = {
        "schema": "keystone.slack.eval_evidence.v1",
        "work_item_id": str(run_provenance.get("work_item_id") or work_item_payload.get("id") or ""),
        "route": str(run_provenance.get("route") or getattr(getattr(result, "route", ""), "value", "") or ""),
        "status": str(run_provenance.get("status") or getattr(getattr(result, "status", ""), "value", "") or ""),
        "permalink": context.permalink,
        "slack_channel_id": source_channel_id,
        "slack_channel_name": source_channel_name,
        "slack_thread_ts": source_thread_ts,
        "slack_context": {
            "channel_id": source_channel_id,
            "channel_name": source_channel_name,
            "thread_ts": source_thread_ts,
            "permalink_present": bool(context.permalink),
        },
        "context_policy": str(
            slack_context.get("prompt_context_layout")
            or slack_context.get("channel_history_policy")
            or (context.metadata or {}).get("context_scope")
            or ""
        ),
        "thread_fetch_status": str(slack_context.get("thread_fetch_status") or context.thread_fetch_status or ""),
        "thread_message_count": thread_message_count,
        "warning_count": len(warnings),
        "warnings": warnings[:8],
        "cost_profile": str(query_prompt.get("cost_profile") or ""),
        "source_count": len(sources),
        "visible_source_count": sum(1 for item in sources if isinstance(item, dict) and item.get("url")),
        "sdk_estimated_cost_usd": _number_or_none(sdk_cost.get("estimated_usd") or sdk_cost.get("amount_usd")),
        "sdk_cache_hit_rate": _number_or_none(sdk_usage.get("cache_hit_rate")),
        "duration_ms": duration_ms,
        "time_to_response_ms": duration_ms,
        "execution": {
            "duration_ms": duration_ms,
            "time_to_response_ms": duration_ms,
        },
        "response_hash": _hash_text(human_summary) if human_summary else "",
        "response_summary_chars": len(human_summary),
        "model_provider": str(model.get("provider") or ""),
        "model_name": str(model.get("name") or model.get("model") or ""),
        "run_mode": str(model.get("run_mode") or ""),
        "search_provider": str(retrieval.get("search_provider") or retrieval.get("provider") or ""),
        "search_provider_sequence": [
            str(item) for item in retrieval.get("search_provider_sequence") or []
        ],
        "prompt_versions": _trace_prompt_versions(result_payload),
        "prompt_metadata": {
            "source": "slack_action_eval_save",
            "route": str(run_provenance.get("route") or ""),
            "status": str(run_provenance.get("status") or ""),
            "context_fingerprint": str(run_provenance.get("context_fingerprint") or ""),
            "requested_task_hash": str(run_provenance.get("requested_task_hash") or ""),
            "work_item_id": str(run_provenance.get("work_item_id") or work_item_payload.get("id") or ""),
        },
    }
    tool_summary = _trace_tool_summary_from_payload(result_payload)
    if tool_summary:
        evidence["tool_summary"] = tool_summary
    orchestrator_summary = _trace_orchestrator_summary_from_payload(result_payload)
    if orchestrator_summary.get("orchestrator"):
        evidence["orchestrator"] = orchestrator_summary["orchestrator"]
    if orchestrator_summary.get("orchestrator_preflight"):
        evidence["orchestrator_preflight"] = orchestrator_summary["orchestrator_preflight"]
    if orchestrator_summary.get("orchestrator_review"):
        evidence["orchestrator_review"] = orchestrator_summary["orchestrator_review"]
    blocker_diagnostics = slack_eval_blocker_diagnostics(result_payload)
    if blocker_diagnostics:
        evidence["blocker_diagnostics"] = blocker_diagnostics
    return evidence


def _latest_sdk_usage_from_result(result: Any) -> dict[str, Any]:
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    candidates: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
        if usage:
            candidates.append(usage)
    return candidates[-1] if candidates else {}


def _latest_sdk_cost_from_result(result: Any) -> dict[str, Any]:
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
    candidates: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        cost = metadata.get("cost") if isinstance(metadata.get("cost"), dict) else {}
        if cost:
            candidates.append(cost)
    return candidates[-1] if candidates else {}


def _trace_prompt_versions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("prompt_versions", "prompt_config_versions"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value[:20] if isinstance(item, dict)]
    return []


def _trace_tool_summary_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    existing = payload.get("tool_summary") if isinstance(payload.get("tool_summary"), dict) else {}
    if existing:
        return existing
    tooling = payload.get("tooling") if isinstance(payload.get("tooling"), dict) else {}
    if tooling:
        return tooling
    counts: dict[str, int] = {}
    failures: dict[str, int] = {}
    statuses: dict[str, set[str]] = {}
    for event in payload.get("events") or []:
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        name = _metadata_scalar(
            metadata.get("tool_name")
            or metadata.get("tool")
            or metadata.get("function_name")
            or event.get("tool_name")
            or event.get("name")
            or ("unknown_tool" if "tool" in event_type or "function" in event_type else "")
        )
        if not name:
            continue
        status = _metadata_scalar(metadata.get("status") or event.get("status") or "")
        failed = bool(
            metadata.get("error")
            or metadata.get("error_type")
            or status.lower() in {"error", "failed", "failure", "timeout"}
        )
        counts[name] = counts.get(name, 0) + 1
        if status:
            statuses.setdefault(name, set()).add(status.lower())
        if failed:
            failures[name] = failures.get(name, 0) + 1
    if not counts:
        return {}
    return {
        "tool_call_count": sum(counts.values()),
        "failed_tool_call_count": sum(failures.values()),
        "tool_names": sorted(counts)[:20],
        "tool_call_summary": [
            {
                "name": name,
                "count": counts[name],
                "failed_count": failures.get(name, 0),
                "status": "failed"
                if failures.get(name, 0)
                else (sorted(statuses.get(name, set()))[-1] if statuses.get(name) else "observed"),
            }
            for name in sorted(counts)[:20]
        ],
    }


def _trace_orchestrator_summary_from_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    preflight = (
        payload.get("orchestrator_preflight")
        if isinstance(payload.get("orchestrator_preflight"), dict)
        else {}
    )
    review = (
        payload.get("orchestrator_review")
        if isinstance(payload.get("orchestrator_review"), dict)
        else {}
    )
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    feedback = payload.get("operator_feedback_requests")
    if not isinstance(feedback, list):
        feedback = []
    preflight_blocker_count = _safe_count(
        preflight.get("blocker_count") or preflight.get("preflight_blocker_count")
    )
    review_feedback_count = _safe_count(
        review.get("feedback_count") or review.get("review_feedback_count")
    )
    result: dict[str, dict[str, Any]] = {}
    orchestrator = {
        "preflight": bool(preflight),
        "review": bool(review),
        "blocker_count": preflight_blocker_count or len(blockers),
        "feedback_count": review_feedback_count or len(feedback),
        "selected_route": _metadata_scalar(
            preflight.get("selected_route")
            or preflight.get("route")
            or payload.get("route")
            or ""
        ),
        "review_status": _metadata_scalar(review.get("status") or review.get("review_status") or ""),
    }
    if any(orchestrator.values()):
        result["orchestrator"] = orchestrator
    if preflight:
        result["orchestrator_preflight"] = {
            "blocker_count": orchestrator["blocker_count"],
            "selected_route": orchestrator["selected_route"],
            "has_preflight": True,
        }
    if review:
        result["orchestrator_review"] = {
            "feedback_count": orchestrator["feedback_count"],
            "review_status": orchestrator["review_status"],
            "has_review": True,
        }
    return result


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _number_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _render_eval_dashboard(database_path: Path) -> dict[str, str]:
    try:
        from promptfoo.eval_dashboard import DEFAULT_DASHBOARD_PATH, render_dashboard
    except ImportError:
        return {}
    try:
        output_path = render_dashboard(database_path=database_path)
    except (OSError, ValueError, sqlite3.Error):
        return {}
    try:
        relative = str(output_path.relative_to(Path.cwd()))
    except ValueError:
        relative = str(output_path)
    return {
        "dashboard_path": str(output_path.resolve()),
        "dashboard_relative_path": relative or str(DEFAULT_DASHBOARD_PATH),
    }


def _eval_dashboard_url() -> str:
    try:
        from promptfoo.eval_urls import eval_dashboard_url
    except ImportError:
        return os.environ.get(
            "KEYSTONE_PROMPTFOO_DASHBOARD_URL",
            "http://127.0.0.1:8769/dashboard",
        ).rstrip("/")
    return eval_dashboard_url()


def _eval_dashboard_case_url(case_id: str) -> str:
    try:
        from promptfoo.eval_urls import eval_dashboard_case_url
    except ImportError:
        base_url = _eval_dashboard_url()
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}case={quote(case_id)}"
    return eval_dashboard_case_url(case_id)


def _eval_review_case_url(case_id: str) -> str:
    try:
        from promptfoo.eval_urls import eval_review_case_url
    except ImportError:
        base_url = _eval_dashboard_url().replace("/dashboard", "/review")
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}case={quote(case_id)}"
    return eval_review_case_url(case_id)


def _append_eval_thread_guidance(summary: str, *, eval_record: dict[str, Any]) -> str:
    case_id = _clean_scalar(eval_record.get("case_id"))
    run_id = _clean_scalar(eval_record.get("run_id"))
    dashboard_case_url = _clean_scalar(eval_record.get("dashboard_case_url"))
    review_case_url = _clean_scalar(eval_record.get("review_case_url"))
    parts = [summary.strip()] if summary.strip() else []
    detail = f"Eval: case `{case_id}`"
    if run_id:
        detail += f", run `{run_id}`"
    detail += "."
    if dashboard_case_url:
        detail += f" Dashboard: <{dashboard_case_url}|case dashboard>."
    if review_case_url:
        detail += f" Review form: <{review_case_url}|score this case>."
    detail += " Score from the linked form, or use `Score with Orchestrator Judge` when enabled, then press `Submit Evaluation` in Slack to save scores and refresh the dashboard."
    parts.append(detail)
    return "\n\n".join(parts).strip()


def _eval_slack_actions(eval_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return renderer-ready Slack actions for one saved eval run."""

    return [
        _eval_review_slack_action(eval_record),
        _eval_orchestrator_judge_slack_action(eval_record),
    ]


def _eval_review_slack_action(eval_record: dict[str, Any]) -> dict[str, Any]:
    """Return the renderer-ready Slack button descriptor for eval scoring."""

    case_id = _clean_scalar(eval_record.get("case_id"))
    run_id = _clean_scalar(eval_record.get("run_id"))
    agent = _clean_scalar(eval_record.get("agent"))
    slack_thread_ts = _clean_scalar(eval_record.get("slack_thread_ts"))
    dashboard_case_url = _clean_scalar(eval_record.get("dashboard_case_url"))
    review_case_url = _clean_scalar(eval_record.get("review_case_url"))
    return {
        "label": "Submit Evaluation",
        "action_id": KBA_EVAL_REVIEW,
        "intent": KBA_INTENT_EVAL_REVIEW,
        "style": "primary",
        "value": business_agent_action_value(
            intent=KBA_INTENT_EVAL_REVIEW,
            metadata={
                "eval_record": {
                    "case_id": case_id,
                    "run_id": run_id,
                    "agent": agent,
                    "slack_thread_ts": slack_thread_ts,
                    "dashboard_case_url": dashboard_case_url,
                    "review_case_url": review_case_url,
                }
            },
        ),
        "metadata": {
            "eval_record": {
                "case_id": case_id,
                "run_id": run_id,
                "agent": agent,
                "slack_thread_ts": slack_thread_ts,
                "dashboard_case_url": dashboard_case_url,
                "review_case_url": review_case_url,
            }
        },
    }


def _eval_orchestrator_judge_slack_action(eval_record: dict[str, Any]) -> dict[str, Any]:
    """Return the Slack button descriptor that triggers Orchestrator judge scoring."""

    case_id = _clean_scalar(eval_record.get("case_id"))
    run_id = _clean_scalar(eval_record.get("run_id"))
    agent = _clean_scalar(eval_record.get("agent"))
    slack_thread_ts = _clean_scalar(eval_record.get("slack_thread_ts"))
    dashboard_case_url = _clean_scalar(eval_record.get("dashboard_case_url"))
    review_case_url = _clean_scalar(eval_record.get("review_case_url"))
    eval_metadata = {
        "eval_record": {
            "case_id": case_id,
            "run_id": run_id,
            "agent": agent,
            "slack_thread_ts": slack_thread_ts,
            "dashboard_case_url": dashboard_case_url,
            "review_case_url": review_case_url,
        }
    }
    return {
        "label": "Score with Orchestrator Judge",
        "action_id": KBA_EVAL_ORCHESTRATOR_JUDGE,
        "intent": KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
        "value": business_agent_action_value(
            intent=KBA_INTENT_EVAL_ORCHESTRATOR_JUDGE,
            metadata=eval_metadata,
        ),
        "metadata": eval_metadata,
    }


def _blocked_slack_agent_result(
    *,
    submission: SlackAgentRunSubmission,
    context: SlackSelectedMessageContext | None,
    context_file_path: str,
    context_warnings: list[str],
    feedback_events: list[dict[str, Any]],
    orchestrator_preflight: Any,
) -> SlackAgentActionResult:
    route_result = orchestrator_preflight.route_result
    request_hash = _hash_text(submission.requested_task)
    run_provenance = {
        "schema": "keystone.slack.agent_run_provenance.v1",
        "context_validated": True,
        "validation_errors": [],
        "work_item_id": "",
        "route": str(route_result.route),
        "status": "blocked",
        "requested_task_hash": request_hash,
        "context_fingerprint": _hash_text(
            json.dumps(
                {
                    "channel_id": _context_value(context, "channel_id"),
                    "request_hash": request_hash,
                    "selected_message_ts": _context_value(context, "selected_message_ts"),
                    "thread_ts": _context_value(context, "thread_ts"),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        ),
        "source_channel_id": _context_value(context, "channel_id"),
        "source_message_ts": _context_value(context, "selected_message_ts"),
        "source_thread_ts": _context_value(context, "thread_ts"),
        "selected_message_ts": _context_value(context, "selected_message_ts"),
        "context_file_path": context_file_path,
    }
    result_payload = {
        "mode": "blocked",
        "status": "blocked",
        "route": str(route_result.route),
        "request_text": submission.requested_task,
        "send_enabled": False,
        "block_kind": orchestrator_preflight.block_kind,
        "block_reason": orchestrator_preflight.block_reason,
        "orchestrator_preflight": compact_orchestrator_preflight_payload(orchestrator_preflight),
        "slack_run_provenance": run_provenance,
        "output": compact_orchestrator_preflight_payload(orchestrator_preflight).get(
            "route_result", {}
        ),
    }
    return SlackAgentActionResult(
        stage="work_item",
        callback_id=RUN_AGENT_VIEW_CALLBACK_ID,
        context_file_path=context_file_path,
        work_item=None,
        route=str(route_result.route),
        status="blocked",
        warnings=context_warnings,
        feedback_events=feedback_events,
        run_provenance=run_provenance,
        result=result_payload,
    )


def _build_feedback_collector(
    feedback_events: list[dict[str, Any]],
    *,
    upstream_callback: Callable[[str, dict[str, Any]], None] | None,
) -> Callable[[str, dict[str, Any]], None]:
    """Collect run feedback for Slack bridges and forward it when requested."""

    upstream_disabled = False

    def collect(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal upstream_disabled
        event = slack_agent_feedback_event(event_type, payload)
        feedback_events.append(event)
        if upstream_callback is None or upstream_disabled:
            return
        try:
            upstream_callback(event_type, payload)
        except Exception as exc:  # noqa: BLE001 - feedback delivery must be best effort.
            upstream_disabled = True
            feedback_events.append(
                slack_agent_feedback_event(
                    "feedback_delivery_failed",
                    {
                        "failed_event_type": event_type,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                        "send_enabled": False,
                    },
                )
            )

    return collect


def _resolve_context_file_path(path: str | Path, *, context_dir: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    if raw_path.is_absolute():
        return raw_path.resolve()
    return (Path(context_dir).expanduser().resolve() / raw_path).resolve()


def _context_value(context: SlackSelectedMessageContext | None, field: str) -> str:
    if context is None:
        return ""
    return str(getattr(context, field, "") or "").strip()


def _orchestrator_workflow_state_from_slack_context(
    context: SlackSelectedMessageContext | None,
    *,
    request_text: str = "",
    database_url: str | None = None,
) -> dict[str, Any]:
    """Build compact Slack context for Orchestrator preflight reasoning."""

    if context is None:
        return {}
    state = {
        "slack_context": {
            "channel_id": context.channel_id,
            "channel_name": context.channel_name,
            "selected_message_ts": context.selected_message_ts,
            "thread_ts": context.thread_ts,
            "thread_fetch_status": context.thread_fetch_status,
            "permalink": context.permalink,
            "warnings": context.warnings,
        },
        "recent_slack_thread": [
            {
                "id": message.ts,
                "source_agent": message.user_id or message.username,
                "summary": _clean_text(message.text, max_chars=320),
            }
            for message in _thread_messages_for_prompt(context)[:8]
            if message.text
        ],
        "slack_thread_transcript": _slack_thread_transcript(
            context,
            latest_request=request_text,
        ),
        "prior_agent_runs": [
            _compact_prior_agent_run(item) for item in context.prior_agent_runs[:5]
        ],
    }
    if _should_include_channel_automation_context(request_text):
        automations = _channel_automation_context(
            context,
            database_url=database_url,
        )
        if automations:
            state["channel_automations"] = automations
    return state


def _slack_query_prompt_for_submission(
    submission: SlackAgentRunSubmission,
    context: SlackSelectedMessageContext | None,
    *,
    manual_request_plan: dict[str, Any] | None,
):
    selected_text = ""
    selected_permalink = ""
    channel_name = ""
    thread_summary = ""
    prior_summaries: list[str] = []
    if context is not None:
        selected_text = context.selected_message.text
        selected_permalink = context.selected_message.permalink or context.permalink
        channel_name = context.channel_name or context.channel_id
        thread_summary = _slack_thread_transcript(
            context,
            latest_request=submission.requested_task,
        )
        prior_summaries = [
            str(item.get("summary") or item.get("title") or "").strip()
            for item in (_compact_prior_agent_run(run) for run in context.prior_agent_runs[:3])
            if str(item.get("summary") or item.get("title") or "").strip()
        ]
    prompt_input = build_slack_query_prompt_input(
        raw_request=submission.requested_task,
        selected_message_text=selected_text,
        selected_message_permalink=selected_permalink,
        channel_name=channel_name,
        thread_summary=thread_summary,
        prior_agent_summaries=prior_summaries,
        manual_plan=manual_request_plan,
    )
    return resolve_slack_query_prompt(prompt_input)


def _should_include_channel_automation_context(request_text: str) -> bool:
    text = str(request_text or "").lower()
    return any(
        marker in text
        for marker in (
            "automation",
            "automations",
            "scheduled",
            "schedule",
            "channel default",
            "channel defaults",
            "digest",
            "weekly",
            "cadence",
            "workflow status",
            "what is running",
            "running here",
            "what runs here",
            "why did this post",
            "why was this posted",
            "state of this channel",
            "what posts here",
            "what is this channel",
        )
    )


def _channel_automation_context(
    context: SlackSelectedMessageContext,
    *,
    database_url: str | None,
) -> list[dict[str, Any]]:
    if not database_url:
        return []
    channel_candidates = [
        value for value in (context.channel_name, context.channel_id) if str(value or "").strip()
    ]
    if not channel_candidates:
        return []
    try:
        report = build_automation_inventory_report(
            database_url=database_url,
            channels=channel_candidates,
            limit=5,
        )
    except Exception:
        return []
    spec_by_id = {spec.id: spec for spec in report.automation_specs}
    items: list[dict[str, Any]] = []
    for binding in report.channel_bindings[:8]:
        spec = spec_by_id.get(binding.automation_id)
        if spec is None:
            continue
        items.append(
            {
                "id": binding.id,
                "automation_id": spec.id,
                "title": spec.name,
                "summary": _clean_text(spec.description, max_chars=260),
                "workflow": spec.workflow,
                "schedule": spec.schedule,
                "route": spec.target_agent,
                "channel": binding.channel_name or binding.channel_id,
                "default_channel": spec.default_channel,
                "purpose": binding.purpose,
                "status": getattr(spec.status, "value", str(spec.status)),
            }
        )
    return items


def _slack_context_sdk_session(
    context: SlackSelectedMessageContext | None,
    *,
    enabled: bool,
) -> Any | None:
    """Build the same Slack-thread SDK session used later by the WorkItem run."""

    if not enabled or context is None:
        return None
    spec = resolve_sdk_session_spec(
        scope="slack",
        components=(
            context.team_id,
            context.channel_id,
            context.thread_ts or context.selected_message_ts,
        ),
        default_enabled=True,
    )
    return build_sdk_session(spec)


def _compact_prior_agent_run(item: dict[str, Any]) -> dict[str, Any]:
    summary = _clean_text(item.get("result_summary") or item.get("result_text"), max_chars=320)
    feedback = (
        item.get("operator_feedback") if isinstance(item.get("operator_feedback"), list) else []
    )
    feedback_summaries = [
        _clean_text(entry.get("text"), max_chars=180)
        for entry in feedback[:3]
        if isinstance(entry, dict) and _clean_text(entry.get("text"), max_chars=180)
    ]
    if feedback_summaries:
        feedback_text = " Operator feedback: " + " | ".join(feedback_summaries)
        summary = _clean_text(f"{summary}{feedback_text}", max_chars=520)
    return {
        "id": _clean_scalar(item.get("run_id") or item.get("id")),
        "route": _clean_scalar(item.get("route")),
        "status": _clean_scalar(item.get("status")),
        "object_id": _clean_scalar(item.get("work_item_id")),
        "title": _clean_text(item.get("request_text"), max_chars=160),
        "summary": summary,
        "created_at": _clean_scalar(item.get("created_at")),
    }


def _slack_run_provenance(
    *,
    submission: SlackAgentRunSubmission,
    context: SlackSelectedMessageContext | None,
    context_file_path: str,
    result: Any,
) -> dict[str, Any]:
    """Return bridge-checkable provenance for rejecting stale Slack run results."""

    work_item = getattr(result, "work_item", None)
    metadata = getattr(getattr(work_item, "target", None), "metadata", {})
    slack_context = metadata.get("slack_context") if isinstance(metadata, dict) else {}
    if not isinstance(slack_context, dict):
        slack_context = {}

    source_channel_id = _first_context_value(context, slack_context, "channel_id")
    source_message_ts = _first_context_value(context, slack_context, "selected_message_ts")
    source_thread_ts = _first_context_value(context, slack_context, "thread_ts")
    request_hash = _hash_text(submission.requested_task)
    fingerprint = _hash_text(
        json.dumps(
            {
                "channel_id": source_channel_id,
                "request_hash": request_hash,
                "selected_message_ts": source_message_ts,
                "thread_ts": source_thread_ts,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    validation_errors = _slack_run_provenance_errors(
        context=context,
        slack_context=slack_context,
        requested_task=submission.requested_task,
        work_item_request_text=str(getattr(work_item, "request_text", "") or ""),
    )
    return {
        "schema": "keystone.slack.agent_run_provenance.v1",
        "work_item_id": str(getattr(work_item, "id", "") or ""),
        "route": str(getattr(getattr(result, "route", ""), "value", getattr(result, "route", ""))),
        "status": str(
            getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
        ),
        "source_channel_id": source_channel_id,
        "source_message_ts": source_message_ts,
        "source_thread_ts": source_thread_ts,
        "requested_task_hash": request_hash,
        "context_fingerprint": fingerprint,
        "context_file_path": context_file_path,
        "context_validated": not validation_errors,
        "validation_errors": validation_errors,
    }


def _slack_run_provenance_errors(
    *,
    context: SlackSelectedMessageContext | None,
    slack_context: dict[str, Any],
    requested_task: str,
    work_item_request_text: str,
) -> list[str]:
    errors: list[str] = []
    if not requested_task.strip():
        errors.append("missing requested task")
    if work_item_request_text.strip() and work_item_request_text.strip() != requested_task.strip():
        errors.append("work_item request_text does not match modal requested task")
    if context is None:
        return errors
    for field in ("channel_id", "selected_message_ts", "thread_ts"):
        expected = str(getattr(context, field, "") or "").strip()
        actual = str(slack_context.get(field) or "").strip()
        if expected and not actual:
            errors.append(f"work_item Slack {field} missing from selected context")
        elif expected and actual and expected != actual:
            errors.append(f"work_item Slack {field} does not match selected context")
    return errors


def _first_context_value(
    context: SlackSelectedMessageContext | None,
    slack_context: dict[str, Any],
    field: str,
) -> str:
    if context is not None:
        value = str(getattr(context, field, "") or "").strip()
        if value:
            return value
    return str(slack_context.get(field) or "").strip()


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _message_from_payload(
    message: dict[str, Any],
    *,
    fallback_ts: str = "",
    fallback_permalink: str = "",
) -> SlackContextMessage:
    return SlackContextMessage(
        ts=_clean_scalar(message.get("ts") or fallback_ts),
        user_id=_clean_scalar(message.get("user") or message.get("bot_id")),
        username=_clean_scalar(message.get("username") or message.get("user_name")),
        text=_clean_text(message.get("text"), max_chars=_MAX_MESSAGE_TEXT_CHARS),
        permalink=_clean_scalar(message.get("permalink") or fallback_permalink),
        metadata={
            "type": _clean_scalar(message.get("type")),
            "subtype": _clean_scalar(message.get("subtype")),
        },
    )


def _eval_metadata_from_payload(
    payload: dict[str, Any],
    message: dict[str, Any],
) -> dict[str, Any]:
    candidates = [
        payload.get("eval"),
        message.get("eval"),
        _nested(payload, "metadata", "eval"),
        _nested(payload, "metadata", "event_payload", "eval"),
        _nested(message, "metadata", "eval"),
        _nested(message, "metadata", "event_payload", "eval"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        case_id = _clean_scalar(candidate.get("case_id"))
        if not case_id:
            continue
        return {
            "case_id": case_id,
            "source": _clean_scalar(candidate.get("source")),
            "visible_in_prompt": bool(candidate.get("visible_in_prompt", False)),
        }
    return {}


def _ordered_thread_messages(raw_messages: list[dict[str, Any]]) -> list[SlackContextMessage]:
    messages, _dropped_old_messages = _ordered_thread_messages_with_policy(raw_messages)
    return messages


def _ordered_thread_messages_with_policy(
    raw_messages: list[dict[str, Any]],
) -> tuple[list[SlackContextMessage], int]:
    messages = [
        _message_from_payload(raw, fallback_permalink="")
        for raw in raw_messages
        if isinstance(raw, dict)
    ]
    by_key: dict[str, SlackContextMessage] = {}
    for index, message in enumerate(messages):
        key = message.ts or f"index:{index}"
        if key not in by_key:
            by_key[key] = message
    ordered = sorted(by_key.values(), key=_message_sort_key)
    latest_ts = max((_slack_ts_sort_value(message.ts) for message in ordered), default=0.0)
    cutoff = latest_ts - _MAX_SLACK_CONTEXT_WINDOW_SECONDS if latest_ts > 0 else 0.0
    recent = [
        message
        for message in ordered
        if not message.ts or _slack_ts_sort_value(message.ts) >= cutoff
    ]
    dropped_old_messages = max(0, len(ordered) - len(recent))
    return recent[-_MAX_THREAD_MESSAGES:], dropped_old_messages


def _thread_messages_for_prompt(context: SlackSelectedMessageContext) -> list[SlackContextMessage]:
    messages = list(context.thread_messages)
    if context.selected_message.text and not any(
        message.ts and message.ts == context.selected_message.ts for message in messages
    ):
        messages.append(context.selected_message)
    return sorted(messages, key=_message_sort_key)


def _slack_thread_transcript(
    context: SlackSelectedMessageContext,
    *,
    latest_request: str = "",
) -> str:
    lines = [
        "Slack thread context (deterministic append-only order):",
        f"Channel: {context.channel_name or context.channel_id}",
        f"Thread TS: {context.thread_ts or context.selected_message_ts}",
    ]
    for index, message in enumerate(_thread_messages_for_prompt(context), start=1):
        speaker = message.username or message.user_id or "unknown"
        text = _clean_text(message.text, max_chars=900)
        if not text:
            continue
        lines.append(f"{index}. [{message.ts}] {speaker}: {text}")
    if latest_request.strip():
        lines.extend(
            [
                "",
                "Latest operator follow-up:",
                _clean_text(latest_request, max_chars=1200),
            ]
        )
    return "\n".join(lines).strip()


def _message_sort_key(message: SlackContextMessage) -> tuple[float, str]:
    return (_slack_ts_sort_value(message.ts), message.ts)


def _slack_ts_sort_value(value: str) -> float:
    try:
        return float(str(value or "0"))
    except (TypeError, ValueError):
        return 0.0


def _modal_context_summary(context: SlackSelectedMessageContext) -> str:
    location = context.channel_name or context.channel_id or "selected Slack context"
    text = _clean_text(context.selected_message.text, max_chars=240)
    warning = f"\nWarning: {context.warnings[0]}" if context.warnings else ""
    return (
        f"Selected context: `{location}` at `{context.selected_message_ts}`\n"
        f"{text or 'No message text was provided by Slack.'}{warning}"
    )


def _first_plain_text_input(view: dict[str, Any]) -> str:
    state = view.get("state") if isinstance(view.get("state"), dict) else {}
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    for block in values.values():
        if not isinstance(block, dict):
            continue
        preferred = block.get(RUN_AGENT_TASK_ACTION_ID)
        if isinstance(preferred, dict):
            text = _clean_text(preferred.get("value"), max_chars=4000)
            if text:
                return text
        for item in block.values():
            if isinstance(item, dict) and item.get("type") == "plain_text_input":
                text = _clean_text(item.get("value"), max_chars=4000)
                if text:
                    return text
    return ""


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _slack_permalink(channel_id: str, ts: str) -> str:
    if not channel_id or not ts:
        return ""
    return f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def _clean_scalar(value: Any) -> str:
    return _clean_text(value, max_chars=500)


def _metadata_scalar(value: Any) -> str:
    return _clean_text(value, max_chars=96)


def _clean_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHAR_RE.sub("", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."
