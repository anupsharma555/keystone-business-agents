"""Preprint/#knowledge-hub context specialist agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.capabilities.tool_scope import (
    ToolScopeMode,
    attach_tool_scope_receipt,
    scope_tools_for_request,
)
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.operational_context import PreprintsContextResult
from keystone_agents.sdk import (
    Agent,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.announcement_context_tools import (
    read_preprint_announcement_evidence,
    retrieve_preprint_announcement_history,
)
from keystone_agents.tools.signal_lifecycle_tools import (
    advance_signal_lifecycle_checkpoint,
    inspect_signal_lifecycle,
    prepare_signal_lifecycle_checkpoint,
)


def _preprints_context_tools(*, tool_tier: str | int | None = None) -> list[Any]:
    tools: list[Any] = [
        retrieve_preprint_announcement_history,
        read_preprint_announcement_evidence,
        inspect_signal_lifecycle,
        prepare_signal_lifecycle_checkpoint,
        advance_signal_lifecycle_checkpoint,
    ]
    if tool_tier is None:
        return tools
    return filter_tools_for_tier("preprints_context_agent", tools, tool_tier)


def build_preprints_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    compact_instructions: bool = False,
    manual_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    tool_scope_mode: ToolScopeMode | str = ToolScopeMode.AUTO,
) -> Agent:
    """Build the preprint/#knowledge-hub context specialist."""

    skill_files = select_agent_skill_names(
        "preprints_context_agent",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    composer = compose_direct_instructions if compact_instructions else compose_instructions
    prompt_files = (
        ("keystone_profile.md", "safety_policy.md", "preprints_context.md")
        if compact_instructions
        else (
            "keystone_profile.md",
            "safety_policy.md",
            "tools.md",
            "preprints_context.md",
        )
    )
    instructions = composer(*prompt_files, skill_files=skill_files)
    resolved_scope_mode = tool_scope_mode
    if str(tool_scope_mode) == ToolScopeMode.AUTO.value and (
        request_text or manual_plan is not None or tool_tier is not None
    ):
        resolved_scope_mode = ToolScopeMode.REQUEST_SCOPED
    attachment = scope_tools_for_request(
        "preprints_context_agent",
        _preprints_context_tools(tool_tier=tool_tier),
        manual_request_plan=manual_plan,
        tool_tier=tool_tier,
        mode=resolved_scope_mode,
    )
    agent = build_sdk_agent(
        name="preprints_context_agent",
        instructions=instructions,
        output_type=PreprintsContextResult,
        tools=list(attachment.tools),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="preprints_context_agent",
        handoff_description=(
            "Use for read-only historical preprint/#knowledge-hub context, recurring "
            "research themes, opportunity signals, and psychiatry-field direction guidance."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)
