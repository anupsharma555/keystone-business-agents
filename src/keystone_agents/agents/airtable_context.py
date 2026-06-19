"""Airtable context specialist agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import AirtableContextResult
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
    airtable_write_record,
)


def _airtable_context_tools(*, tool_tier: str | int | None = None) -> list[Any]:
    tools: list[Any] = [
        airtable_get_base_schema,
        airtable_read_records,
        airtable_write_record,
    ]
    if tool_tier is None:
        return tools
    return filter_tools_for_tier("airtable_context_agent", tools, tool_tier)


def build_airtable_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the Airtable context specialist."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "airtable_context.md",
        skill_files=select_agent_skill_names(
            "airtable_context_agent",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="airtable_context_agent",
        instructions=instructions,
        output_type=AirtableContextResult,
        tools=_airtable_context_tools(tool_tier=tool_tier),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="airtable_context_agent",
        handoff_description=(
            "Use for Airtable base, table, field, and candidate record context, "
            "plus direct approved create/update writes when invoked as the selected agent."
        ),
    )
