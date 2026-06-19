"""Zotero context specialist agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import ZoteroContextResult
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.internal_data_tools import (
    google_doc_read,
    google_doc_write,
    google_drive_create_folder,
    google_drive_get_file_metadata,
    google_drive_list_folder,
    google_drive_search_files,
    google_sheet_append_rows,
    google_sheet_create,
    google_sheet_list,
    google_sheet_read_table,
    google_sheet_update_row,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.zotero_context_tools import (
    zotero_import_article_with_backend,
    zotero_read_api_metadata,
    zotero_resolve_article_context,
    zotero_resolve_collection_context,
)


def _zotero_context_tools(*, tool_tier: str | int | None = None) -> list[Any]:
    tools: list[Any] = [
        list_local_context_sources,
        search_local_context,
        read_local_context_file,
        zotero_resolve_collection_context,
        zotero_resolve_article_context,
        zotero_read_api_metadata,
        zotero_import_article_with_backend,
        google_drive_list_folder,
        google_drive_search_files,
        google_drive_get_file_metadata,
        google_doc_read,
        google_doc_write,
        google_drive_create_folder,
        google_sheet_list,
        google_sheet_create,
        google_sheet_read_table,
        google_sheet_append_rows,
        google_sheet_update_row,
    ]
    if tool_tier is None:
        return tools
    return filter_tools_for_tier("zotero_context_agent", tools, tool_tier)


def build_zotero_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the Zotero context specialist."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "local_context.md",
        "zotero_context.md",
        skill_files=select_agent_skill_names(
            "zotero_context_agent",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="zotero_context_agent",
        instructions=instructions,
        output_type=ZoteroContextResult,
        tools=_zotero_context_tools(tool_tier=tool_tier),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="zotero_context_agent",
        handoff_description=(
            "Use for Zotero library, collection, item, article, importer, and "
            "evidence context, plus direct approved Workspace artifact writes."
        ),
    )
