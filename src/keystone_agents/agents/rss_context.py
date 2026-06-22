"""RSS/#announcements context specialist agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import RssContextResult
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.announcement_context_tools import retrieve_rss_announcement_history


def _rss_context_tools(*, tool_tier: str | int | None = None) -> list[Any]:
    tools: list[Any] = [retrieve_rss_announcement_history]
    if tool_tier is None:
        return tools
    return filter_tools_for_tier("rss_context_agent", tools, tool_tier)


def build_rss_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the RSS/#announcements context specialist."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "rss_context.md",
        skill_files=select_agent_skill_names(
            "rss_context_agent",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="rss_context_agent",
        instructions=instructions,
        output_type=RssContextResult,
        tools=_rss_context_tools(tool_tier=tool_tier),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="rss_context_agent",
        handoff_description=(
            "Use for read-only historical RSS/#announcements context, recurring "
            "themes, opportunity signals, and future-direction guidance."
        ),
    )
