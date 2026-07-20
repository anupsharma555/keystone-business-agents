"""Bounded diagnostics for Slack eval runtime evidence."""

from __future__ import annotations

import re
from typing import Any

from keystone_agents.storage.sqlite_store import redact_secrets

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)


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
    if not next_action and result_payload.get("next_safe_action"):
        next_action = {"description": result_payload.get("next_safe_action")}
    status = str(result_payload.get("status") or "").strip()
    block_kind = _clean_text(result_payload.get("block_kind"), max_chars=120)
    block_reason = _clean_text(result_payload.get("block_reason"), max_chars=500)
    if not block_reason and blockers:
        block_reason = _clean_text(blockers[0].get("message"), max_chars=500)
    readiness_gates = _blocked_readiness_gate_names(result_payload)
    if not blockers and not next_action and not block_kind and not block_reason and status != "blocked":
        return {}
    blocker_codes = [_clean_text(blocker.get("code"), max_chars=120) for blocker in blockers]
    blocker_messages = [_clean_text(blocker.get("message"), max_chars=500) for blocker in blockers]
    blocker_codes = [code for code in blocker_codes if code]
    blocker_messages = [message for message in blocker_messages if message]
    return {
        "schema": "keystone.slack.eval_blocker_diagnostics.v1",
        "diagnostic_category": "workflow_blocker" if status == "blocked" or blockers else "",
        "block_kind": block_kind or ("work_item_blocker" if blockers else "blocked_status"),
        "blocker_count": len(blockers),
        "blocker_codes": blocker_codes[:8],
        "block_reason": block_reason or (blocker_messages[0] if blocker_messages else ""),
        "blocker_messages": blocker_messages[:4],
        "next_action": _next_action_payload(next_action),
        "readiness_gate_names": readiness_gates,
    }


def slack_eval_child_step_summary(
    result_payload: dict[str, Any],
    tool_summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a capped ordered timeline without prompts, summaries, or tool I/O."""

    steps: list[dict[str, Any]] = []
    for raw in result_payload.get("execution_steps") or []:
        if not isinstance(raw, dict):
            continue
        name = _step_label(raw.get("name"))
        category = _step_label(raw.get("category"))
        if not name or not category:
            continue
        steps.append(
            {
                "step_index": len(steps) + 1,
                "category": category,
                "name": name,
                "status": _step_label(raw.get("status")) or "observed",
                "duration_ms": _safe_nonnegative_float(raw.get("duration_ms")),
                "error_kind": _step_label(raw.get("error_kind")),
                "provider": _step_label(raw.get("provider")),
                "request_count": _safe_nonnegative_int(raw.get("request_count")),
                "source_count": _safe_nonnegative_int(raw.get("source_count")),
                "visible_source_count": _safe_nonnegative_int(
                    raw.get("visible_source_count")
                ),
                "estimated_cost_usd": _safe_nonnegative_float(
                    raw.get("estimated_cost_usd")
                ),
                "cache_hit_rate": _safe_rate(raw.get("cache_hit_rate")),
                "approval_required": bool(raw.get("approval_required")),
                "blocker_count": _safe_nonnegative_int(raw.get("blocker_count")),
            }
        )
    has_tool_steps = any(step["category"] == "tool" for step in steps)
    if not has_tool_steps and isinstance(tool_summary, dict):
        for tool in tool_summary.get("tool_call_summary") or []:
            if not isinstance(tool, dict):
                continue
            name = _step_label(tool.get("name"))
            if not name:
                continue
            steps.append(
                {
                    "step_index": len(steps) + 1,
                    "category": "tool",
                    "name": name,
                    "status": _step_label(tool.get("status")) or "observed",
                    "duration_ms": None,
                    "error_kind": "tool_failure"
                    if _safe_nonnegative_int(tool.get("failed_count"))
                    else "",
                    "provider": "",
                    "request_count": _safe_nonnegative_int(tool.get("count")),
                    "source_count": 0,
                    "visible_source_count": 0,
                    "estimated_cost_usd": None,
                    "cache_hit_rate": None,
                    "approval_required": False,
                    "blocker_count": 0,
                }
            )
            if len(steps) >= 40:
                break
    return steps[:40]


def _blocked_readiness_gate_names(result_payload: dict[str, Any]) -> list[str]:
    context_pack = result_payload.get("context_pack")
    if not isinstance(context_pack, dict):
        work_item = result_payload.get("work_item")
        context_pack = work_item.get("context_pack") if isinstance(work_item, dict) else {}
    if not isinstance(context_pack, dict):
        return []
    names: list[str] = []
    for gate in context_pack.get("readiness_gates") or []:
        if not isinstance(gate, dict) or gate.get("ready") is not False:
            continue
        name = _clean_text(gate.get("name"), max_chars=120)
        if name and name not in names:
            names.append(name)
    return names[:8]


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
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", str(redacted or ""))
    text = " ".join(text.split())
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _step_label(value: object) -> str:
    text = str(value or "").strip()
    return text[:120] if re.fullmatch(r"[A-Za-z0-9_.:+/-]{1,120}", text) else ""


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_nonnegative_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _safe_rate(value: object) -> float | None:
    rate = _safe_nonnegative_float(value)
    return min(1.0, rate) if rate is not None else None
