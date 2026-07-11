"""Google Workspace context specialist agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import GoogleWorkspaceContextResult
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.internal_data_tools import (
    google_doc_read,
    google_doc_trash,
    google_doc_write,
    google_drive_create_folder,
    google_drive_get_file_metadata,
    google_drive_list_folder,
    google_drive_remove_folder,
    google_drive_rename_folder,
    google_drive_search_files,
    google_sheet_append_rows,
    google_sheet_create,
    google_sheet_create_tab,
    google_sheet_delete_rows,
    google_sheet_list,
    google_sheet_read_table,
    google_sheet_remove_tab,
    google_sheet_trash,
    google_sheet_update_row,
    google_sheet_update_tab,
    google_slide_deck_read,
    presentation_delete_test_artifact_local,
    presentation_extract_slide_copy_local,
    presentation_read_local,
    presentation_search_local,
)


def _google_workspace_context_tools(*, tool_tier: str | int | None = None) -> list[Any]:
    tools: list[Any] = [
        google_drive_list_folder,
        google_drive_search_files,
        google_drive_get_file_metadata,
        google_slide_deck_read,
        presentation_search_local,
        presentation_read_local,
        presentation_extract_slide_copy_local,
        presentation_delete_test_artifact_local,
        google_doc_read,
        google_doc_write,
        google_doc_trash,
        google_drive_create_folder,
        google_drive_rename_folder,
        google_drive_remove_folder,
        google_sheet_list,
        google_sheet_create,
        google_sheet_read_table,
        google_sheet_append_rows,
        google_sheet_update_row,
        google_sheet_delete_rows,
        google_sheet_create_tab,
        google_sheet_update_tab,
        google_sheet_remove_tab,
        google_sheet_trash,
    ]
    if tool_tier is None:
        return tools
    return filter_tools_for_tier("google_workspace_context_agent", tools, tool_tier)


def build_google_workspace_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
) -> Agent:
    """Build the Google Workspace context specialist."""

    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "google_workspace_context.md",
        skill_files=select_agent_skill_names(
            "google_workspace_context_agent",
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all_skills,
        ),
    )
    return build_sdk_agent(
        name="google_workspace_context_agent",
        instructions=instructions,
        output_type=GoogleWorkspaceContextResult,
        tools=_google_workspace_context_tools(tool_tier=tool_tier),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="google_workspace_context_agent",
        handoff_description=(
            "Use for Google Drive, Docs, Sheets, and file metadata context, plus "
            "direct approved Workspace writes when invoked as the selected agent."
        ),
    )
