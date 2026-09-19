"""Zotero context specialist agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.capabilities.tool_scope import (
    ToolScopeMode,
    attach_tool_scope_receipt,
    scope_tools_for_request,
)
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.planning.compatibility import positive_capability_text
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
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

_ZOTERO_READ_TOOLS_BY_RESOURCE: dict[str, frozenset[str]] = {
    "zotero_collection": frozenset(
        {"zotero_resolve_collection_context", "zotero_read_api_metadata"}
    ),
    "zotero_item": frozenset(
        {"zotero_resolve_article_context", "zotero_read_api_metadata"}
    ),
    "zotero_note": frozenset(
        {"zotero_read_api_metadata", "zotero_read_item_children"}
    ),
    "zotero_attachment": frozenset(
        {
            "zotero_read_api_metadata",
            "zotero_read_item_children",
            "zotero_read_pdf_attachment_text",
        }
    ),
}

_ZOTERO_WRITE_TOOLS_BY_ACTION: dict[tuple[str, str], frozenset[str]] = {
    ("create", "zotero_collection"): frozenset({"zotero_write_test_collection"}),
    ("update", "zotero_collection"): frozenset({"zotero_write_test_collection"}),
    ("delete", "zotero_collection"): frozenset({"zotero_delete_test_collection"}),
    ("create", "zotero_note"): frozenset({"zotero_write_test_note"}),
    ("update", "zotero_note"): frozenset({"zotero_write_test_note"}),
    ("delete", "zotero_note"): frozenset({"zotero_delete_test_note"}),
    ("create", "zotero_item"): frozenset({"zotero_write_test_item"}),
    ("update", "zotero_item"): frozenset({"zotero_write_test_item"}),
    ("delete", "zotero_item"): frozenset({"zotero_delete_test_item"}),
}


def _canonical_zotero_tool_names(
    authority: ExecutionIntentAuthority,
    *,
    request_text: str,
) -> set[str]:
    """Map canonical provider-object steps to the smallest Zotero tool set."""

    plan = authority.plan
    if plan is None or not authority.authorizes_provider("zotero"):
        return set()
    operations = set(authority.effective_provider_operations("zotero"))
    if not operations:
        return set()
    steps = authority.provider_action_steps("zotero")
    normalized = " ".join(str(request_text or "").lower().split())
    marked_test = bool(
        re.search(r"\bkba_test(?:_[a-z0-9]+)*\b", normalized)
        or re.search(r"\bmarked\s+(?:standalone\s+)?zotero\s+test\b", normalized)
    )
    if steps:
        note_operations = {
            step.operation for step in steps if step.resource_type == "zotero_note"
        }
        if marked_test and {"create", "update", "delete"} <= note_operations:
            return {"zotero_test_note_lifecycle"}
        selected: set[str] = set()
        for step in steps:
            if step.operation in {"read", "search", "verify"}:
                selected.update(
                    _ZOTERO_READ_TOOLS_BY_RESOURCE.get(
                        step.resource_type,
                        frozenset(),
                    )
                )
                continue
            if (
                step.operation == "create"
                and step.resource_type == "zotero_item"
                and plan.target_type == "zotero_article"
            ):
                selected.add("zotero_import_article_with_backend")
                continue
            selected.update(
                _ZOTERO_WRITE_TOOLS_BY_ACTION.get(
                    (step.operation, step.resource_type),
                    frozenset(),
                )
            )
        return selected

    if (
        marked_test
        and {"create", "update", "delete"} <= operations
        and re.search(r"\bnotes?\b", normalized)
    ):
        return {"zotero_test_note_lifecycle"}

    # Compatibility for older canonical producers that supplied typed provider
    # operations but not resource-level steps. Target type may narrow the read;
    # otherwise stay within Zotero read-only tools and never infer a write family
    # from prose.
    selected: set[str] = set()
    resource_type = {
        "zotero_collection": "zotero_collection",
        "zotero_article": "zotero_item",
    }.get(plan.target_type, "")
    if operations & {"read", "search", "verify"}:
        if resource_type:
            selected.update(_ZOTERO_READ_TOOLS_BY_RESOURCE[resource_type])
        else:
            for names in _ZOTERO_READ_TOOLS_BY_RESOURCE.values():
                selected.update(names)
    if resource_type and operations & {"create", "update", "delete"}:
        for operation in operations & {"create", "update", "delete"}:
            if (
                operation == "create"
                and resource_type == "zotero_item"
                and plan.target_type == "zotero_article"
            ):
                selected.add("zotero_import_article_with_backend")
            else:
                selected.update(
                    _ZOTERO_WRITE_TOOLS_BY_ACTION.get(
                        (operation, resource_type),
                        frozenset(),
                    )
                )
    return selected


def _zotero_context_tools(
    *,
    tool_tier: str | int | None = None,
    request_text: str = "",
    manual_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
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
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.invalid:
        return []
    if tool_tier is None and authority.plan is None:
        return tools
    zotero_operations = set(authority.effective_provider_operations("zotero"))
    resolved_tool_tier = (
        tool_tier
        if tool_tier is not None
        else "internal_write"
        if zotero_operations.intersection({"create", "update", "delete", "attach"})
        else "core_read"
    )
    filtered = filter_tools_for_tier(
        "zotero_context_agent",
        tools,
        resolved_tool_tier,
    )
    if authority.canonical:
        selected_names = _canonical_zotero_tool_names(
            authority,
            request_text=request_text,
        )
        return [
            tool for tool in filtered if getattr(tool, "name", "") in selected_names
        ]
    normalized = " ".join(positive_capability_text(request_text).lower().split())
    tier = str(resolved_tool_tier)
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
                return [
                    tool
                    for tool in filtered
                    if getattr(tool, "name", "") == "zotero_test_note_lifecycle"
                ]
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
    manual_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
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
    candidate_tools = (
        _zotero_context_tools(
            tool_tier=tool_tier,
            request_text=request_text,
            manual_plan=manual_plan,
        )
        if attach_tools
        else []
    )
    attachment = scope_tools_for_request(
        "zotero_context_agent",
        candidate_tools,
        manual_request_plan=manual_plan,
        tool_tier=tool_tier,
        mode=(
            ToolScopeMode.REQUEST_SCOPED
            if manual_plan is not None
            else ToolScopeMode.FULL
        ),
    )
    agent = build_sdk_agent(
        name="zotero_context_agent",
        instructions=instructions,
        output_type=ZoteroContextResult,
        tools=list(attachment.tools),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="zotero_context_agent",
        handoff_description=(
            "Use for Zotero library, collection, item, article, importer, and "
            "evidence context, plus direct approved Workspace artifact writes."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)
