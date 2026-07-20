"""Zotero context specialist agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import ZoteroContextResult
from keystone_agents.sdk import (
    Agent,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
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
    zotero_delete_test_collection,
    zotero_delete_test_item,
    zotero_delete_test_note,
    zotero_import_article_with_backend,
    zotero_list_cached_items,
    zotero_read_api_metadata,
    zotero_read_item_children,
    zotero_read_pdf_attachment_text,
    zotero_resolve_article_context,
    zotero_resolve_collection_context,
    zotero_test_note_lifecycle,
    zotero_write_test_collection,
    zotero_write_test_item,
    zotero_write_test_note,
)


def _zotero_context_tools(
    *,
    tool_tier: str | int | None = None,
    request_text: str = "",
) -> list[Any]:
    tools: list[Any] = [
        list_local_context_sources,
        search_local_context,
        read_local_context_file,
        zotero_resolve_collection_context,
        zotero_resolve_article_context,
        zotero_list_cached_items,
        zotero_read_api_metadata,
        zotero_read_item_children,
        zotero_read_pdf_attachment_text,
        zotero_import_article_with_backend,
        zotero_write_test_note,
        zotero_delete_test_note,
        zotero_test_note_lifecycle,
        zotero_write_test_collection,
        zotero_delete_test_collection,
        zotero_write_test_item,
        zotero_delete_test_item,
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
    filtered = filter_tools_for_tier("zotero_context_agent", tools, tool_tier)
    normalized = " ".join(str(request_text or "").lower().split())
    tier = str(tool_tier)
    if tier not in {"core_read", "internal_write"}:
        return filtered
    selected_names: set[str] = set()
    if re.search(r"\b(?:article|item|title|author|abstract|doi|journal|publication)\b", normalized):
        selected_names.update({"zotero_read_api_metadata", "zotero_resolve_article_context"})
    if re.search(r"\bnotes?|attachments?|pdf|full\s+text|paper\b", normalized):
        selected_names.update({"zotero_read_api_metadata", "zotero_read_item_children"})
    if re.search(r"\bpdf|full\s+text|paper\b", normalized):
        selected_names.add("zotero_read_pdf_attachment_text")
    if re.search(r"\bcollections?\b", normalized):
        selected_names.update({"zotero_resolve_collection_context", "zotero_read_api_metadata"})
    if re.search(r"\b(?:cached|cache|local)\b", normalized):
        selected_names.update({"zotero_list_cached_items", "zotero_resolve_article_context"})
    if tier == "internal_write":
        marked_test = bool(
            re.search(r"\b(?:kba_test(?:_[a-z0-9]+)*|marked|disposable|test)\b", normalized)
        )
        if re.search(
            r"\b(?:import|save)\b[^.\n]{0,120}\b(?:article|url|doi)\b",
            normalized,
        ) or re.search(r"\badd\b[^.\n]{0,80}\b(?:url|doi)\b", normalized):
            selected_names.add("zotero_import_article_with_backend")
        if marked_test and re.search(r"\bnotes?\b", normalized):
            requests_create = bool(re.search(r"\b(?:create|write|add)\b", normalized))
            requests_update = bool(
                re.search(r"\b(?:change|edit|modify|revise|update)\b", normalized)
            )
            requests_delete = bool(
                re.search(r"\b(?:delete|remove|clean\s*up)\b", normalized)
            )
            lifecycle = "lifecycle" in normalized or (
                requests_create and requests_update and requests_delete
            )
            if lifecycle:
                selected_names.add("zotero_test_note_lifecycle")
            elif requests_create or requests_update:
                selected_names.add("zotero_write_test_note")
            if not lifecycle and requests_delete:
                selected_names.add("zotero_delete_test_note")
        if marked_test and re.search(r"\bcollections?\b", normalized):
            if re.search(r"\b(?:create|write|add|edit|modify|rename|update)\b", normalized):
                selected_names.add("zotero_write_test_collection")
            if re.search(r"\b(?:delete|remove|clean\s*up)\b", normalized):
                selected_names.add("zotero_delete_test_collection")
        if marked_test and re.search(r"\b(?:items?|webpages?)\b", normalized):
            if re.search(r"\b(?:create|write|add|edit|modify|revise|update)\b", normalized):
                selected_names.add("zotero_write_test_item")
            if re.search(r"\b(?:delete|remove|clean\s*up)\b", normalized):
                selected_names.add("zotero_delete_test_item")
        if re.search(r"\b(?:google\s+docs?|document)\b", normalized):
            selected_names.update(
                {
                    "google_drive_search_files",
                    "google_drive_get_file_metadata",
                    "google_doc_read",
                    "google_doc_write",
                }
            )
        if re.search(r"\b(?:google\s+sheets?|spreadsheet|worksheet|rows?)\b", normalized):
            selected_names.update(
                {
                    "google_sheet_list",
                    "google_sheet_read_table",
                    "google_sheet_create",
                    "google_sheet_append_rows",
                    "google_sheet_update_row",
                }
            )
    if not selected_names:
        return filtered
    return [tool for tool in filtered if getattr(tool, "name", "") in selected_names]


def build_zotero_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    attach_tools: bool = True,
    compact_instructions: bool = False,
) -> Agent:
    """Build the Zotero context specialist."""

    skill_files = select_agent_skill_names(
        "zotero_context_agent",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    if compact_instructions:
        instructions = compose_direct_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "zotero_context.md",
            skill_files=skill_files,
        )
    else:
        instructions = compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "tools.md",
            "local_context.md",
            "zotero_context.md",
            skill_files=skill_files,
        )
    return build_sdk_agent(
        name="zotero_context_agent",
        instructions=instructions,
        output_type=ZoteroContextResult,
        tools=(
            _zotero_context_tools(tool_tier=tool_tier, request_text=request_text)
            if attach_tools
            else []
        ),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="zotero_context_agent",
        handoff_description=(
            "Use for Zotero library, collection, item, article, importer, and "
            "evidence context, plus direct approved Workspace artifact writes."
        ),
    )
