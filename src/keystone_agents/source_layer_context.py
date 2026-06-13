"""Runtime source-layer context for SDK specialist inputs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.tool_availability import runtime_tool_availability_for_agent


def runtime_source_layer_policy_context(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return compact source-layer guidance for an agent's current tool surface."""

    key = str(agent_name or "").strip()
    spec = AGENT_REGISTRY.get(key)
    if spec is None:
        return {
            "agent_name": key,
            "status": "unknown_agent",
            "available": False,
            "layers": [],
        }
    availability = runtime_tool_availability_for_agent(
        key,
        tools=spec.tools,
        optional_tools=spec.optional_tools,
        env=os.environ if env is None else env,
    )
    source_layer_policy = availability.get("source_layer_policy")
    if not isinstance(source_layer_policy, Mapping):
        return {
            "agent_name": key,
            "status": "missing",
            "available": False,
            "layers": [],
        }
    layers = [
        _compact_source_layer(layer)
        for layer in source_layer_policy.get("layers", [])
        if isinstance(layer, Mapping)
    ]
    return {
        "agent_name": key,
        "status": str(source_layer_policy.get("status") or "unknown"),
        "available": bool(source_layer_policy.get("available")),
        "layers": layers,
        "reason": str(source_layer_policy.get("reason") or ""),
    }


def runtime_source_layer_policy_text(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Render compact source-layer guidance for typed SDK prompt context."""

    context = runtime_source_layer_policy_context(agent_name, env=env)
    layers = context.get("layers", [])
    if not isinstance(layers, list) or not layers:
        return ""
    lines = [
        "Runtime source-layer policy:",
        (
            "Use this as advisory tool-boundary context only. Answer from the latest "
            "user question and actual tool outputs."
        ),
    ]
    for layer in layers:
        if not isinstance(layer, Mapping):
            continue
        name = str(layer.get("layer") or "").strip()
        if not name:
            continue
        lines.append(
            "- "
            f"{name}: status={layer.get('runtime_status')}; "
            f"available={bool(layer.get('runtime_available'))}; "
            f"use_for={_join_policy_items(layer.get('use_for'))}; "
            f"not_for={_join_policy_items(layer.get('not_for'))}; "
            f"reasoning_contract={_compact_contract(layer.get('reasoning_contract'))}"
        )
    return "\n".join(lines).strip()


def append_runtime_source_layer_policy_text(
    text: str,
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Append runtime source-layer guidance to an existing context string once."""

    base = str(text or "").strip()
    if "Runtime source-layer policy:" in base:
        return base
    policy_text = runtime_source_layer_policy_text(agent_name, env=env)
    if not policy_text:
        return base
    return "\n\n".join(item for item in (base, policy_text) if item).strip()


def _compact_source_layer(layer: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "layer": str(layer.get("layer") or ""),
        "runtime_tool": str(layer.get("runtime_tool") or ""),
        "runtime_status": str(layer.get("runtime_status") or ""),
        "runtime_available": bool(layer.get("runtime_available")),
        "runtime_configured": bool(layer.get("runtime_configured")),
        "use_for": [
            str(item)
            for item in layer.get("use_for", [])
            if str(item).strip()
        ],
        "not_for": [
            str(item)
            for item in layer.get("not_for", [])
            if str(item).strip()
        ],
        "reasoning_contract": str(layer.get("reasoning_contract") or ""),
    }


def _join_policy_items(value: Any) -> str:
    if not isinstance(value, list):
        return "-"
    items = [str(item).strip() for item in value if str(item).strip()]
    return " | ".join(items) if items else "-"


def _compact_contract(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    return text[:320]
