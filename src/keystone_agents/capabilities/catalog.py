"""Model-visible metadata derived from the existing registry and attached capabilities."""

from __future__ import annotations

import json
import re
from typing import Any

CATALOG_PROMPT_FILE = "runtime_capability_catalog.md"
CATALOG_MARKER = "<!-- runtime_capability_catalog.json -->"
STATIC_INSTRUCTIONS_ATTRIBUTE = "keystone_static_instructions"
_SKILL_BODY_MARKER = re.compile(r"<!-- ([a-z0-9_]+)/SKILL\.md -->")


def static_instruction_text(agent: Any) -> str | None:
    """Return the static base of KBA's existing replay-aware instruction callable.

    Unknown callables are explicitly unresolved; their function repr is never
    hashed or counted as prompt text, and callbacks are never invoked here.
    """

    instructions = getattr(agent, "instructions", "")
    seen: set[int] = set()
    while callable(instructions) and id(instructions) not in seen:
        seen.add(id(instructions))
        base = getattr(instructions, STATIC_INSTRUCTIONS_ATTRIBUTE, None)
        if base is None:
            break
        instructions = base
    return None if callable(instructions) else str(instructions or "")


def runtime_capability_catalog(agent: Any) -> dict[str, Any] | None:
    """Describe this agent's attached surfaces without evaluating or granting access."""

    if not getattr(agent, "keystone_runtime_catalog_enabled", False):
        return None
    # Lazy imports keep the SDK constructor and static registry importable independently.
    from keystone_agents.agent_registry import AGENT_REGISTRY
    from keystone_agents.agent_tool_policy import tool_name_for_policy
    from keystone_agents.sdk import list_skill_metadata
    from keystone_agents.skill_sets import SPECIALIST_SKILL_NAMES

    route = str(getattr(agent, "name", "") or "")
    spec = AGENT_REGISTRY.get(route)
    if spec is None:
        return None
    static_base = static_instruction_text(agent)
    loaded = set(_SKILL_BODY_MARKER.findall(static_base or ""))
    own_skill_names = tuple(
        skill for skill in spec.skills
        if skill in loaded or skill == SPECIALIST_SKILL_NAMES.get(route)
    )
    own_skills = [
        {
            "id": metadata.skill_id,
            "description": metadata.purpose,
            "reference": metadata.reference,
            "body_loaded": metadata.skill_id in loaded if static_base is not None else None,
        }
        for metadata in list_skill_metadata(own_skill_names)
    ]
    attached_tools: list[dict[str, Any]] = []
    specialist_tools: list[dict[str, Any]] = []
    for tool in list(getattr(agent, "tools", []) or []):
        enabled = getattr(tool, "is_enabled", True)
        if enabled is False:
            continue
        tool_name = tool_name_for_policy(tool)
        if not tool_name:
            continue
        availability = "conditional" if callable(enabled) else "attached"
        attached_tools.append({"name": tool_name, "availability": availability})
        child_route = getattr(tool, "specialist_route_name", None)
        child = AGENT_REGISTRY.get(child_route) if isinstance(child_route, str) else None
        if child is not None:
            specialist_tools.append({
                "route": child.route_name,
                "name": child.agent_name,
                "description": child.handoff_description,
                "invocation_tool": tool_name,
                "mode": str(getattr(tool, "specialist_tool_mode", "") or "unspecified"),
                "availability": availability,
            })
    attached_handoffs: list[dict[str, Any]] = []
    for handoff in list(getattr(agent, "handoffs", []) or []):
        enabled = getattr(handoff, "is_enabled", True)
        if enabled is False:
            continue
        target = str(
            getattr(handoff, "agent_name", "") or getattr(handoff, "name", "") or ""
        )
        target_spec = AGENT_REGISTRY.get(target)
        if target_spec is None:
            continue
        attached_handoffs.append({
            "route": target_spec.route_name,
            "name": target_spec.agent_name,
            "description": target_spec.handoff_description,
            "invocation_tool": str(getattr(handoff, "tool_name", "") or ""),
            "availability": "conditional" if callable(enabled) else "attached",
        })
    output_type = getattr(agent, "output_type", None)
    return {
        "owner": route,
        "output_contract": getattr(output_type, "__name__", type(output_type).__name__),
        "own_skills": own_skills,
        "attached_tools": attached_tools,
        "attached_specialist_tools": specialist_tools,
        "attached_handoffs": attached_handoffs,
    }


def append_runtime_capability_catalog(agent: Any, instructions: str) -> str:
    """Append a fresh metadata-only view to already resolved instructions."""

    catalog = runtime_capability_catalog(agent)
    if catalog is None:
        return instructions
    from keystone_agents.sdk import load_prompt

    policy = load_prompt(CATALOG_PROMPT_FILE).strip()
    encoded = json.dumps(catalog, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return (
        f"{instructions}\n\n<!-- {CATALOG_PROMPT_FILE} -->\n{policy}"
        f"\n\n{CATALOG_MARKER}\n{encoded}"
    )


def instruction_profile_text(agent: Any) -> str | None:
    """Project the static prompt plus current catalog for fingerprints and byte accounting.

    Chief's replayed provider evidence stays outside this static cache grouping;
    the SDK still validates the actual full prefix before reusing cached tokens.
    """

    base = static_instruction_text(agent)
    return append_runtime_capability_catalog(agent, base) if base is not None else None
