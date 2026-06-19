"""Environment contract for passing Orchestrator preflight into child agents."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.temporal_policy import temporal_depth_policy

ORCHESTRATOR_PREFLIGHT_ENV = "KEYSTONE_ORCHESTRATOR_PREFLIGHT_JSON"
MANUAL_REQUEST_PLAN_ENV = "KEYSTONE_MANUAL_REQUEST_PLAN_JSON"
ORCHESTRATOR_ROUTE_RESULT_ENV = "KEYSTONE_ORCHESTRATOR_ROUTE_RESULT_JSON"

_PREFLIGHT_HANDOFF_KEYS = (
    "request_text",
    "requested_agent",
    "advisory_only",
    "selected_agent",
    "blocked_by_orchestrator",
    "execution_allowed",
    "block_kind",
    "block_reason",
)

_ROUTE_RESULT_HANDOFF_KEYS = (
    "route",
    "target_agent",
    "workflow",
    "routing_mode",
    "rationale",
    "clarification_request",
    "stop_reason",
    "requires_human_review",
    "approval_required",
    "approval_state",
    "approval_scope",
    "approval_rationale",
    "external_use_approval_required",
    "approved_context_present",
    "refused",
    "send_enabled",
    "can_send_email",
    "forbidden_actions",
    "state_context_used",
    "audit_notes",
)


def _json_payload(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def orchestrator_preflight_env(preflight: Any | None) -> dict[str, str]:
    """Return environment values for a child specialist process."""

    payload = compact_orchestrator_preflight_payload(preflight)
    if not payload:
        return {}
    env = {ORCHESTRATOR_PREFLIGHT_ENV: _json_payload(payload)}
    manual_plan = payload.get("manual_request_plan")
    if manual_plan:
        env[MANUAL_REQUEST_PLAN_ENV] = _json_payload(manual_plan)
    route_result = payload.get("route_result")
    if route_result:
        env[ORCHESTRATOR_ROUTE_RESULT_ENV] = _json_payload(route_result)
    return env


def compact_orchestrator_preflight_payload(preflight: Any | None) -> dict[str, Any]:
    """Return the bounded Orchestrator handoff safe for child env/stdout payloads."""

    if preflight is None:
        return {}
    payload = preflight.model_dump(mode="json") if hasattr(preflight, "model_dump") else preflight
    if not isinstance(payload, Mapping):
        return {}
    compact: dict[str, Any] = {
        key: payload[key]
        for key in _PREFLIGHT_HANDOFF_KEYS
        if key in payload and payload[key] not in (None, "")
    }
    manual_plan = payload.get("manual_request_plan")
    if manual_plan:
        compact["manual_request_plan"] = (
            manual_plan.model_dump(mode="json")
            if hasattr(manual_plan, "model_dump")
            else manual_plan
        )
    route_result = _compact_route_result(payload.get("route_result"))
    if route_result:
        compact["route_result"] = route_result
    sdk_usage_events = _compact_sdk_usage_events(payload.get("sdk_usage_events"))
    if sdk_usage_events:
        compact["sdk_usage_events"] = sdk_usage_events
    compact["preflight_memo"] = _preflight_memo_payload(compact)
    return compact


def _compact_sdk_usage_events(events: Any | None) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    compact_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        compact: dict[str, Any] = {}
        for key in ("agent_name", "run_stage"):
            value = event.get(key)
            if value not in (None, ""):
                compact[key] = value
        for key in ("usage", "cost", "request_cache"):
            value = event.get(key)
            if isinstance(value, Mapping):
                compact[key] = dict(value)
        if compact:
            compact_events.append(compact)
    return compact_events


def _compact_route_result(route_result: Any | None) -> dict[str, Any]:
    if route_result is None:
        return {}
    payload = (
        route_result.model_dump(mode="json")
        if hasattr(route_result, "model_dump")
        else route_result
    )
    if not isinstance(payload, Mapping):
        return {}
    compact = {
        key: payload[key]
        for key in _ROUTE_RESULT_HANDOFF_KEYS
        if key in payload and payload[key] not in (None, "")
    }
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, Mapping):
        compact["artifacts"] = {
            key: artifacts[key]
            for key in (
                "company_id",
                "opportunity_ids",
                "gmail_thread_ids",
                "crm_ready_fields",
                "notes",
            )
            if key in artifacts and artifacts[key] not in (None, "", [])
        }
    return compact


def _preflight_memo_payload(preflight: Mapping[str, Any]) -> dict[str, Any]:
    route_result = preflight.get("route_result")
    if not isinstance(route_result, Mapping):
        route_result = {}
    return {
        "raw_request": preflight.get("request_text"),
        "manual_request_plan": preflight.get("manual_request_plan"),
        "advisory_only": preflight.get("advisory_only"),
        "selected_agent": preflight.get("selected_agent"),
        "execution_allowed": preflight.get("execution_allowed"),
        "block_kind": preflight.get("block_kind"),
        "block_reason": preflight.get("block_reason"),
        "orchestrator_route": route_result.get("route"),
        "orchestrator_rationale": route_result.get("rationale"),
        "orchestrator_refused": route_result.get("refused"),
        "orchestrator_stop_reason": route_result.get("stop_reason"),
        "temporal_depth_policy": temporal_depth_policy(str(preflight.get("request_text") or "")),
        "source_visibility_requirement": (
            "If the specialist answer includes source-backed external facts, current "
            "claims, dates, deadlines, rates, filings, policies, company facts, roles, "
            "or opportunity signals, include source URLs in the first user-visible "
            "answer. Structured sources alone are not enough."
        ),
    }


def load_orchestrator_preflight_from_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Load parent Orchestrator preflight payload from the process environment."""

    raw = (environ or os.environ).get(ORCHESTRATOR_PREFLIGHT_ENV)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def load_manual_request_plan_from_env(
    environ: Mapping[str, str] | None = None,
) -> ManualRequestPlan | None:
    """Load the parent Orchestrator manual plan from the process environment."""

    raw = (environ or os.environ).get(MANUAL_REQUEST_PLAN_ENV)
    if not raw:
        return None
    try:
        return ManualRequestPlan.model_validate_json(raw)
    except ValueError:
        return None


def apply_orchestrator_preflight_to_args(args: Any) -> Any:
    """Attach parent Orchestrator preflight context to an argparse namespace."""

    preflight = load_orchestrator_preflight_from_env()
    manual_plan = load_manual_request_plan_from_env()
    args.orchestrator_preflight = preflight
    if manual_plan is not None and not getattr(args, "manual_request_plan", None):
        args.manual_request_plan = manual_plan.model_dump(mode="json")
    return args


def attach_orchestrator_preflight_payload(payload: dict[str, Any], args: Any) -> dict[str, Any]:
    """Add Orchestrator preflight fields to a JSON payload when available."""

    preflight = getattr(args, "orchestrator_preflight", None)
    if isinstance(preflight, dict):
        payload["orchestrator_preflight"] = compact_orchestrator_preflight_payload(preflight)
    manual_plan = getattr(args, "manual_request_plan", None)
    if manual_plan and "manual_request_plan" not in payload:
        payload["manual_request_plan"] = manual_plan
    return payload


def orchestrator_preflight_context_text(args: Any) -> str:
    """Return a compact specialist-readable Orchestrator memo."""

    preflight = getattr(args, "orchestrator_preflight", None)
    manual_plan = getattr(args, "manual_request_plan", None)
    if not isinstance(preflight, dict) and not manual_plan:
        return ""
    if isinstance(preflight, dict) and isinstance(preflight.get("preflight_memo"), dict):
        memo = preflight["preflight_memo"]
        return "Orchestrator preflight memo for this specialist run:\n" + json.dumps(
            memo, ensure_ascii=True, sort_keys=True
        )
    route_result = preflight.get("route_result") if isinstance(preflight, dict) else {}
    if not isinstance(route_result, dict):
        route_result = {}
    memo = {
        "raw_request": preflight.get("request_text") if isinstance(preflight, dict) else None,
        "manual_request_plan": manual_plan,
        "advisory_only": preflight.get("advisory_only") if isinstance(preflight, dict) else None,
        "selected_agent": preflight.get("selected_agent") if isinstance(preflight, dict) else None,
        "orchestrator_route": route_result.get("route"),
        "orchestrator_rationale": route_result.get("rationale"),
        "orchestrator_refused": route_result.get("refused"),
    }
    return "Orchestrator preflight memo for this specialist run:\n" + json.dumps(
        memo, ensure_ascii=True, sort_keys=True
    )
