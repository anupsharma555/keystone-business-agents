"""Stable names for exposing Keystone specialists as SDK tools."""

from __future__ import annotations

SPECIALIST_AGENT_TOOL_SUFFIX = "_as_specialist_tool"
ORCHESTRATOR_BUSINESS_RESEARCH_TOOL_NAME = "business_research_analyst_research_brief"
ORCHESTRATOR_OPPORTUNITY_SCOUT_TOOL_NAME = "opportunity_scout_read_only"


def specialist_agent_tool_name(route_name: str) -> str:
    """Return the default manager-facing tool name for one specialist route."""

    return f"{str(route_name or '').strip()}{SPECIALIST_AGENT_TOOL_SUFFIX}"


def is_specialist_agent_tool_name(tool_name: str) -> bool:
    """Return true for registry-derived specialist-as-tool names."""

    return str(tool_name or "").strip().endswith(SPECIALIST_AGENT_TOOL_SUFFIX)
