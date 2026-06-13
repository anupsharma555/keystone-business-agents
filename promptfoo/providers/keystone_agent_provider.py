"""Promptfoo provider for Keystone Business Agents dry-run Slack asks."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run the repo-local ask_agent CLI and return a compact JSON output string."""

    config = options.get("config") or {}
    vars_ = context.get("vars") or {}
    request_text = str(vars_.get("user_input") or prompt).strip()
    if not request_text:
        return _provider_error("missing user_input")

    python = str(config.get("python") or ".venv/bin/python")
    agent = str(vars_.get("agent") or config.get("agent") or "orchestrator")
    max_manager_steps = int(vars_.get("max_manager_steps") or config.get("max_manager_steps") or 1)
    timeout_seconds = int(vars_.get("timeout_seconds") or config.get("timeout_seconds") or 90)

    command = [
        _resolve_project_path(python),
        str(PROJECT_ROOT / "scripts" / "ask_agent.py"),
    ]
    if request_text.lower().startswith("@kni") or request_text.startswith("<@"):
        command.append(request_text)
    else:
        command.extend(["--input", request_text, "--agent", agent])
    command.extend(["--json", "--max-manager-steps", str(max_manager_steps)])
    if bool(vars_.get("live_sdk") or config.get("live_sdk")):
        command.append("--live-sdk")
    else:
        command.append("--no-live-sdk")

    context_file = _write_slack_context(vars_)
    if context_file:
        command.extend(["--context-file", str(context_file)])

    started = time.monotonic()
    env = os.environ.copy()
    env.update(
        {
            "KEYSTONE_PROMPTFOO_EVAL": "true",
            "KEYSTONE_EVAL_SURFACE": str(vars_.get("surface") or "slack"),
        }
    )
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _provider_error(f"ask_agent timed out after {timeout_seconds}s", stderr=str(exc))

    elapsed_seconds = round(time.monotonic() - started, 3)
    if completed.returncode != 0:
        return _provider_error(
            f"ask_agent exited {completed.returncode}",
            stderr=completed.stderr,
            stdout=completed.stdout,
            elapsed_seconds=elapsed_seconds,
        )

    try:
        raw_payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _provider_error(
            f"ask_agent returned non-json output: {exc}",
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_seconds=elapsed_seconds,
        )

    compact = _compact_run_payload(raw_payload)
    compact.update(
        {
            "provider_status": "ok",
            "elapsed_seconds": elapsed_seconds,
            "surface": str(vars_.get("surface") or "slack"),
        }
    )
    return {"output": json.dumps(compact, ensure_ascii=True, sort_keys=True)}


def _resolve_project_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


def _provider_error(message: str, **extra: Any) -> dict[str, str]:
    payload = {"provider_status": "error", "error": message}
    payload.update({key: value for key, value in extra.items() if value})
    return {"output": json.dumps(payload, ensure_ascii=True, sort_keys=True)}


def _write_slack_context(vars_: dict[str, Any]) -> Path | None:
    slack_context = vars_.get("slack_context")
    if not isinstance(slack_context, dict):
        return None

    selected_message = slack_context.get("selected_message")
    if not isinstance(selected_message, dict):
        selected_message = {
            "ts": slack_context.get("selected_message_ts") or "1800000000.000100",
            "user_id": slack_context.get("user_id") or "U_EVAL",
            "username": slack_context.get("username") or "eval-user",
            "text": vars_.get("user_input") or "",
            "permalink": slack_context.get("permalink") or "",
        }

    payload = {
        "schema": "keystone.slack.selected_message_context.v1",
        "channel_id": slack_context.get("channel_id") or "C0BA17Y9C01",
        "channel_name": slack_context.get("channel_name") or "evals",
        "selected_message_ts": slack_context.get("selected_message_ts")
        or selected_message.get("ts")
        or "1800000000.000100",
        "thread_ts": slack_context.get("thread_ts") or selected_message.get("ts") or "",
        "permalink": slack_context.get("permalink") or selected_message.get("permalink") or "",
        "selected_message": selected_message,
        "thread_messages": slack_context.get("thread_messages") or [],
        "metadata": {
            "source": "promptfoo",
            "case_id": vars_.get("case_id") or "",
            "context_scope": "selected_message_or_thread_recent_window",
        },
    }

    directory = Path(tempfile.mkdtemp(prefix="kba-promptfoo-slack-"))
    context_file = directory / "slack-context.json"
    context_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return context_file


def _compact_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    work_item = payload.get("work_item") if isinstance(payload.get("work_item"), dict) else {}
    route_result = _nested(payload, "orchestrator_preflight", "route_result")
    sources = _collect_sources(payload, work_item)
    artifact_refs = _collect_artifacts(payload, work_item)
    audit_notes = _as_strings(payload.get("audit_notes")) + _as_strings(
        work_item.get("audit_notes")
    )
    manager_efficiency = _nested(work_item, "target", "metadata", "manager_loop_efficiency")
    next_action = payload.get("next_action") or work_item.get("next_action") or {}
    route = (
        payload.get("route")
        or work_item.get("current_route")
        or route_result.get("route")
        or ""
    )
    source_types = [
        source.get("source_type") for source in sources if source.get("source_type")
    ]
    artifact_types = [
        artifact.get("artifact_type")
        for artifact in artifact_refs
        if artifact.get("artifact_type")
    ]
    live_sdk = (
        bool(manager_efficiency.get("live_sdk"))
        if isinstance(manager_efficiency, dict)
        else False
    )
    live_search = (
        bool(manager_efficiency.get("live_search"))
        if isinstance(manager_efficiency, dict)
        else False
    )

    return {
        "status": payload.get("status") or work_item.get("status") or "",
        "route": route,
        "target_agent": route_result.get("target_agent") or "",
        "human_summary": _human_summary(payload),
        "source_count": len(sources),
        "source_urls": [source.get("url") for source in sources if source.get("url")],
        "source_titles": [source.get("title") for source in sources if source.get("title")],
        "source_types": source_types,
        "artifact_count": len(artifact_refs),
        "artifact_types": artifact_types,
        "audit_notes": audit_notes,
        "blockers": payload.get("blockers") or work_item.get("blockers") or [],
        "block_kind": _nested(payload, "orchestrator_preflight", "block_kind")
        or payload.get("block_kind")
        or "",
        "refused": bool(route_result.get("refused") or payload.get("mode") == "blocked"),
        "approval_required": bool(route_result.get("approval_required")),
        "external_use_approval_required": bool(route_result.get("external_use_approval_required")),
        "can_send_email": bool(route_result.get("can_send_email")),
        "send_enabled": bool(route_result.get("send_enabled")),
        "forbidden_actions": route_result.get("forbidden_actions") or [],
        "workflow": route_result.get("workflow") or [],
        "next_action_agent": next_action.get("agent") if isinstance(next_action, dict) else "",
        "next_action_requires_approval": bool(
            next_action.get("requires_approval") if isinstance(next_action, dict) else False
        ),
        "manager_review": _nested(work_item, "target", "metadata", "orchestrator_reviews"),
        "manager_review_scores": [
            item.get("overall_score")
            for item in _nested(work_item, "target", "metadata", "orchestrator_reviews")
            if isinstance(item, dict) and item.get("overall_score") is not None
        ],
        "final_synthesis_executed": bool(
            manager_efficiency.get("final_synthesis_executed")
            if isinstance(manager_efficiency, dict)
            else False
        ),
        "live_sdk": live_sdk,
        "live_search": live_search,
        "context_pack_type": _nested(payload, "context_pack", "pack_type"),
        "slack_context_attached": bool(
            _nested(work_item, "target", "metadata", "slack_context")
            or _nested(payload, "context_pack", "target", "metadata", "slack_context")
        ),
    }


def _collect_sources(payload: dict[str, Any], work_item: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    raw_sources = (
        list(work_item.get("sources") or [])
        + list(_nested(payload, "context_pack", "ordered_sources") or [])
        + _artifact_source_refs(payload, work_item)
    )
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        url = str(raw_source.get("url") or "")
        title = str(raw_source.get("title") or "")
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "url": url,
                "title": title,
                "source_type": str(raw_source.get("source_type") or ""),
            }
        )
    return sources


def _collect_artifacts(payload: dict[str, Any], work_item: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    artifacts: list[dict[str, Any]] = []
    raw_artifacts = list(payload.get("artifact_refs") or []) + list(
        work_item.get("artifact_refs") or []
    )
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        key = str(raw_artifact.get("artifact_id") or raw_artifact.get("title") or raw_artifact)
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(raw_artifact)
    return artifacts


def _artifact_source_refs(
    payload: dict[str, Any],
    work_item: dict[str, Any],
) -> list[dict[str, Any]]:
    source_refs: list[dict[str, Any]] = []
    for artifact in _collect_artifacts(payload, work_item):
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        for source_ref in metadata.get("source_refs") or []:
            if isinstance(source_ref, dict):
                source_refs.append(source_ref)
    return source_refs


def _human_summary(payload: dict[str, Any]) -> str:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    blockers = payload.get("blockers")
    if not blockers and isinstance(payload.get("work_item"), dict):
        blockers = payload["work_item"].get("blockers")
    blocker_messages = [
        str(item.get("message"))
        for item in blockers or []
        if isinstance(item, dict) and item.get("message")
    ]
    return str(
        payload.get("human_summary")
        or payload.get("message")
        or output.get("clarification_request")
        or output.get("rationale")
        or " ".join(blocker_messages)
        or ""
    )


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {} if key != keys[-1] else None
        current = current.get(key)
    return current if current is not None else {}


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
