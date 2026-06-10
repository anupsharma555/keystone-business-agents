"""Slack message-action helpers for starting Keystone WorkItem runs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.automation_inventory import build_automation_inventory_report
from keystone_agents.orchestrator.preflight_context import (
    compact_orchestrator_preflight_payload,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.sdk_sessions import build_sdk_session, resolve_sdk_session_spec
from keystone_agents.slack_action_contract import (
    RUN_AGENT_MESSAGE_CALLBACK_ID,
    RUN_AGENT_TASK_ACTION_ID,
    RUN_AGENT_TASK_BLOCK_ID,
    RUN_AGENT_VIEW_CALLBACK_ID,
    SLACK_SELECTED_CONTEXT_SCHEMA,
    slack_agent_feedback_event,
)
from keystone_agents.slack_interactions import parse_slack_interaction_payload
from keystone_agents.workflow_runner import advance_work_item_manager_loop

DEFAULT_SLACK_CONTEXT_DIR = Path("artifacts/slack_contexts")

_MAX_MESSAGE_TEXT_CHARS = 4000
_MAX_THREAD_MESSAGES = 20
_MAX_SLACK_CONTEXT_WINDOW_DAYS = 7
_MAX_SLACK_CONTEXT_WINDOW_SECONDS = _MAX_SLACK_CONTEXT_WINDOW_DAYS * 24 * 60 * 60
_MAX_PRIVATE_METADATA_CHARS = 2800
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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
    path.write_text(
        json.dumps(
            context.model_dump(mode="json", by_alias=True),
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
    }
    return {
        "schema": context.schema_,
        "context_file_path": _clean_text(context_file_path, max_chars=1800),
        "selected_context": selected_context,
        "channel_id": _metadata_scalar(context.channel_id),
        "selected_message_ts": _metadata_scalar(context.selected_message_ts),
        "thread_ts": _metadata_scalar(context.thread_ts),
        "warnings": ["Slack modal metadata reduced to context pointer."],
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
        orchestrator_preflight = run_orchestrator_preflight(
            submission.requested_task,
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
        result = advance_work_item_manager_loop(
            WorkflowRunRequest(
                request_text=submission.requested_task,
                save=True,
                database_url=database_url,
                live_search=live_search,
                live_sdk=live_sdk,
                max_results=max_results,
                context_file_path=context_file_path,
                manual_request_plan=orchestrator_preflight.manual_request_plan.model_dump(
                    mode="json"
                ),
                orchestrator_preflight=compact_orchestrator_preflight_payload(
                    orchestrator_preflight
                ),
                **_slack_cost_conservation_request_options(submission.requested_task),
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
            result=result_payload,
        )
    raise ValueError(f"Unsupported Slack run-agent payload type: {payload_type or 'unknown'}")


def _slack_cost_conservation_request_options(request_text: str) -> dict[str, Any]:
    """Let the WorkItem runner choose route-aware Slack cost controls."""

    return {}


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
