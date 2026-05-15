"""Slack message-action helpers for starting Keystone WorkItem runs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.slack_interactions import parse_slack_interaction_payload
from keystone_agents.workflow_runner import advance_work_item

RUN_AGENT_MESSAGE_CALLBACK_ID = "keystone_run_agent_message"
RUN_AGENT_VIEW_CALLBACK_ID = "keystone_run_agent_submit"
RUN_AGENT_TASK_BLOCK_ID = "keystone_agent_task_block"
RUN_AGENT_TASK_ACTION_ID = "keystone_agent_task"
SLACK_SELECTED_CONTEXT_SCHEMA = "keystone.slack.selected_message_context.v1"
DEFAULT_SLACK_CONTEXT_DIR = Path("artifacts/slack_contexts")

_MAX_MESSAGE_TEXT_CHARS = 4000
_MAX_THREAD_MESSAGES = 20
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
    bounded_thread = [
        _message_from_payload(raw, fallback_permalink="")
        for raw in (provided_thread_messages or [])[:_MAX_THREAD_MESSAGES]
        if isinstance(raw, dict)
    ]
    warnings: list[str] = []
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
            "context_scope": "selected_message_or_thread",
        },
    )


def write_selected_message_context_file(
    context: SlackSelectedMessageContext,
    *,
    directory: str | Path = DEFAULT_SLACK_CONTEXT_DIR,
) -> Path:
    """Write selected Slack context to a local JSON file and return the path."""

    context_dir = Path(directory)
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
    path = context_dir / filename
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

    metadata = {
        "schema": context.schema_,
        "context_file_path": str(context_file_path) if context_file_path else "",
        "channel_id": context.channel_id,
        "selected_message_ts": context.selected_message_ts,
        "thread_ts": context.thread_ts,
        "permalink": context.permalink,
        "warnings": context.warnings,
    }
    return {
        "type": "modal",
        "callback_id": RUN_AGENT_VIEW_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Run Keystone Agent"},
        "submit": {"type": "plain_text", "text": "Run"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(metadata, ensure_ascii=True, sort_keys=True),
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
        if context_file_path:
            context_warnings = load_selected_message_context_file(context_file_path).warnings
        result = advance_work_item(
            WorkflowRunRequest(
                request_text=submission.requested_task,
                save=True,
                database_url=database_url,
                live_search=live_search,
                live_sdk=live_sdk,
                max_results=max_results,
                context_file_path=context_file_path,
            )
        )
        return SlackAgentActionResult(
            stage="work_item",
            callback_id=RUN_AGENT_VIEW_CALLBACK_ID,
            context_file_path=context_file_path,
            work_item=result.work_item.model_dump(mode="json"),
            route=result.route.value,
            status=result.status.value,
            warnings=context_warnings,
            result=result.model_dump(mode="json"),
        )
    raise ValueError(f"Unsupported Slack run-agent payload type: {payload_type or 'unknown'}")


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


def _clean_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHAR_RE.sub("", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."
