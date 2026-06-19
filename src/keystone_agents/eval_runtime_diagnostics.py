"""Bounded diagnostics for Slack eval runtime evidence."""

from __future__ import annotations

from typing import Any

from keystone_agents.storage.sqlite_store import redact_secrets


def slack_eval_blocker_diagnostics(result_payload: dict[str, Any]) -> dict[str, Any]:
    """Return compact blocker diagnostics safe for eval evidence JSON."""

    blockers = _list_of_mappings(result_payload.get("blockers"))
    work_item = result_payload.get("work_item") if isinstance(result_payload.get("work_item"), dict) else {}
    work_item_blockers = _list_of_mappings(work_item.get("blockers"))
    if not blockers and work_item_blockers:
        blockers = work_item_blockers
    next_action = result_payload.get("next_action")
    if not isinstance(next_action, dict):
        next_action = work_item.get("next_action") if isinstance(work_item.get("next_action"), dict) else {}
    status = str(result_payload.get("status") or "").strip()
    if not blockers and not next_action and status != "blocked":
        return {}
    blocker_codes = [_clean_text(blocker.get("code"), max_chars=120) for blocker in blockers]
    blocker_messages = [_clean_text(blocker.get("message"), max_chars=500) for blocker in blockers]
    blocker_codes = [code for code in blocker_codes if code]
    blocker_messages = [message for message in blocker_messages if message]
    return {
        "schema": "keystone.slack.eval_blocker_diagnostics.v1",
        "diagnostic_category": "workflow_blocker" if status == "blocked" or blockers else "",
        "block_kind": "work_item_blocker" if blockers else "blocked_status",
        "blocker_count": len(blockers),
        "blocker_codes": blocker_codes[:8],
        "block_reason": blocker_messages[0] if blocker_messages else "",
        "blocker_messages": blocker_messages[:4],
        "next_action": _next_action_payload(next_action),
    }


def _next_action_payload(next_action: dict[str, Any]) -> dict[str, Any]:
    if not next_action:
        return {}
    return {
        "action": _clean_text(next_action.get("action"), max_chars=160),
        "agent": _clean_text(next_action.get("agent"), max_chars=120),
        "description": _clean_text(next_action.get("description"), max_chars=500),
        "requires_approval": bool(next_action.get("requires_approval")),
    }


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _clean_text(value: object, *, max_chars: int) -> str:
    redacted = redact_secrets(str(value or ""))
    text = " ".join(str(redacted or "").split())
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text
