"""Google Workspace context specialist agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import GoogleWorkspaceContextResult
from keystone_agents.sdk import (
    Agent,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.internal_data_tools import (
    google_doc_read,
    google_doc_test_lifecycle,
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


def _google_workspace_context_tools(
    *,
    tool_tier: str | int | None = None,
    request_text: str = "",
) -> list[Any]:
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
        google_doc_test_lifecycle,
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
    filtered = filter_tools_for_tier("google_workspace_context_agent", tools, tool_tier)
    normalized = " ".join(str(request_text or "").lower().split())
    tier = str(tool_tier)
    if tier not in {"core_read", "internal_write"}:
        return filtered
    selected_names: set[str] = set()
    doc_request = bool(re.search(r"\b(?:google\s+docs?|document)\b", normalized))
    sheet_request = bool(
        re.search(r"\b(?:google\s+sheets?|spreadsheet|worksheet|tab|rows?|cells?)\b", normalized)
    )
    slide_request = bool(re.search(r"\b(?:slides?|presentation|deck)\b", normalized))
    drive_request = bool(re.search(r"\b(?:drive|folder|files?|metadata)\b", normalized))
    if doc_request:
        marked_test = bool(re.search(r"\bkba_test_doc(?:_[a-z0-9]+)*\b", normalized))
        requests_create = bool(re.search(r"\b(?:create|make|write|add)\b", normalized))
        requests_trash = bool(re.search(r"\b(?:delete|remove|trash)\b", normalized))
        if tier == "internal_write" and marked_test and requests_create and requests_trash:
            selected_names.add("google_doc_test_lifecycle")
            return [
                tool
                for tool in filtered
                if getattr(tool, "name", "") == "google_doc_test_lifecycle"
            ]
        else:
            selected_names.update(
                {
                    "google_drive_search_files",
                    "google_drive_get_file_metadata",
                    "google_doc_read",
                }
            )
            if tier == "internal_write" and re.search(
                r"\b(?:create|make|write|add|append|edit|modify|revise|update|replace)\b",
                normalized,
            ):
                selected_names.add("google_doc_write")
            if tier == "internal_write" and requests_trash:
                selected_names.add("google_doc_trash")
    if sheet_request:
        selected_names.update(
            {"google_drive_search_files", "google_sheet_list", "google_sheet_read_table"}
        )
        if tier == "internal_write":
            if re.search(r"\b(?:create|new)\b", normalized) and not re.search(
                r"\b(?:tab|worksheet)\b", normalized
            ):
                selected_names.add("google_sheet_create")
            if re.search(r"\b(?:append|add|insert)\b[^.\n]{0,80}\brows?\b", normalized):
                selected_names.add("google_sheet_append_rows")
            if re.search(
                r"\b(?:edit|modify|revise|update|replace)\b[^.\n]{0,80}\brows?\b",
                normalized,
            ):
                selected_names.add("google_sheet_update_row")
            if re.search(r"\b(?:delete|remove)\b[^.\n]{0,80}\brows?\b", normalized):
                selected_names.add("google_sheet_delete_rows")
            if re.search(r"\b(?:create|add|new)\b[^.\n]{0,80}\b(?:tab|worksheet)\b", normalized):
                selected_names.add("google_sheet_create_tab")
            if re.search(
                r"\b(?:edit|modify|rename|update)\b[^.\n]{0,80}\b(?:tab|worksheet)\b",
                normalized,
            ):
                selected_names.add("google_sheet_update_tab")
            if re.search(r"\b(?:delete|remove)\b[^.\n]{0,80}\b(?:tab|worksheet)\b", normalized):
                selected_names.add("google_sheet_remove_tab")
            if re.search(
                r"\b(?:delete|remove|trash)\b[^.\n]{0,80}\b(?:sheet|spreadsheet)\b",
                normalized,
            ):
                selected_names.add("google_sheet_trash")
    if slide_request:
        selected_names.update(
            {
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_slide_deck_read",
                "presentation_search_local",
                "presentation_read_local",
            }
        )
        if tier == "internal_write" and re.search(r"\b(?:extract|copy)\b", normalized):
            selected_names.add("presentation_extract_slide_copy_local")
        if tier == "internal_write" and re.search(r"\b(?:delete|remove)\b", normalized):
            selected_names.add("presentation_delete_test_artifact_local")
    if drive_request:
        selected_names.update(
            {
                "google_drive_list_folder",
                "google_drive_search_files",
                "google_drive_get_file_metadata",
            }
        )
        if tier == "internal_write" and re.search(
            r"\b(?:create|add|new)\b[^.\n]{0,80}\bfolder\b",
            normalized,
        ):
            selected_names.add("google_drive_create_folder")
        if tier == "internal_write" and re.search(
            r"\b(?:rename|update)\b[^.\n]{0,80}\bfolder\b",
            normalized,
        ):
            selected_names.add("google_drive_rename_folder")
        if tier == "internal_write" and re.search(
            r"\b(?:delete|remove)\b[^.\n]{0,80}\bfolder\b",
            normalized,
        ):
            selected_names.add("google_drive_remove_folder")
    if not selected_names:
        return filtered
    return [tool for tool in filtered if getattr(tool, "name", "") in selected_names]


def build_google_workspace_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    compact_instructions: bool = False,
) -> Agent:
    """Build the Google Workspace context specialist."""

    skill_files = select_agent_skill_names(
        "google_workspace_context_agent",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    composer = compose_direct_instructions if compact_instructions else compose_instructions
    prompt_files = (
        ("keystone_profile.md", "safety_policy.md", "google_workspace_context.md")
        if compact_instructions
        else (
            "keystone_profile.md",
            "safety_policy.md",
            "tools.md",
            "google_workspace_context.md",
        )
    )
    instructions = composer(*prompt_files, skill_files=skill_files)
    return build_sdk_agent(
        name="google_workspace_context_agent",
        instructions=instructions,
        output_type=GoogleWorkspaceContextResult,
        tools=_google_workspace_context_tools(
            tool_tier=tool_tier,
            request_text=request_text,
        ),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="google_workspace_context_agent",
        handoff_description=(
            "Use for Google Drive, Docs, Sheets, and file metadata context, plus "
            "direct approved Workspace writes when invoked as the selected agent."
        ),
    )
