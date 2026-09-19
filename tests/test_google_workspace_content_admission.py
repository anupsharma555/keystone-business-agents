"""Offline admission contract for substantive Google Workspace reads."""

from __future__ import annotations

from typing import Any

from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.planning.compatibility import infer_manual_request_plan

_WORKSPACE_CONTENT_READERS = {
    "google_doc_read",
    "google_sheet_list",
    "google_sheet_read_table",
    "google_slide_deck_read",
    "google_drive_media_ocr_read",
}

_WORKSPACE_SUBSTANTIVE_READERS = _WORKSPACE_CONTENT_READERS - {
    "google_sheet_list",
}

_WORKSPACE_MUTATION_TOOLS = {
    "google_doc_write",
    "google_doc_trash",
    "google_doc_test_lifecycle",
    "google_drive_create_folder",
    "google_drive_rename_folder",
    "google_drive_remove_folder",
    "google_sheet_create",
    "google_sheet_append_rows",
    "google_sheet_update_row",
    "google_sheet_delete_rows",
    "google_sheet_create_tab",
    "google_sheet_update_tab",
    "google_sheet_remove_tab",
    "google_sheet_trash",
    "google_slide_deck_write",
    "presentation_extract_slide_copy_local",
    "presentation_delete_test_artifact_local",
}


def _tool_names(agent: Any) -> set[str]:
    return {
        str(getattr(tool, "name", ""))
        for tool in list(getattr(agent, "tools", []) or [])
        if str(getattr(tool, "name", ""))
    }


def _workspace_plan_and_tools(prompt: str) -> tuple[Any, set[str]]:
    plan = infer_manual_request_plan(
        prompt,
        requested_agent="google_workspace_context_agent",
    )
    agent = build_google_workspace_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )
    return plan, _tool_names(agent)


def test_unknown_drive_artifact_exposes_bounded_mime_reader_bundle() -> None:
    prompt = (
        "Find the latest Northstar operating plan in Google Drive and summarize "
        "its decisions, owners, and unresolved risks. Please do not change anything."
    )

    plan, names = _workspace_plan_and_tools(prompt)

    assert plan.provider_operations == ["read"]
    assert {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        *_WORKSPACE_CONTENT_READERS,
    } <= names
    assert _WORKSPACE_MUTATION_TOOLS.isdisjoint(names)
    assert {"presentation_search_local", "presentation_read_local"}.isdisjoint(names)


def test_explicit_metadata_only_request_excludes_all_content_readers() -> None:
    prompt = (
        "Find the latest Northstar file in Google Drive and give me only its file "
        "name, MIME type, owner, and modified time. Do not open or read its contents, "
        "and do not change anything."
    )

    plan, names = _workspace_plan_and_tools(prompt)

    assert plan.provider_operations == ["read"]
    assert names == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
    }


def test_generic_content_contract_requires_one_successful_mime_reader() -> None:
    prompt = (
        "Find the latest Northstar operating plan in Google Drive and summarize "
        "its decisions, owners, and unresolved risks. Please do not change anything."
    )
    plan, names = _workspace_plan_and_tools(prompt)

    contract = cli._context_agent_tool_execution_contract(
        "google_workspace_context_agent",
        input_text=prompt,
        selected_tool_names=sorted(names),
        manual_plan=plan,
    )

    assert contract is not None
    groups = {group.name: set(group.any_of_tool_names) for group in contract.required_groups}
    assert any(
        _WORKSPACE_SUBSTANTIVE_READERS <= tool_names
        and "google_sheet_list" not in tool_names
        for tool_names in groups.values()
    )
    assert any("google_drive_search_files" in tool_names for tool_names in groups.values())


def test_metadata_only_contract_does_not_require_a_content_reader() -> None:
    prompt = (
        "Find the latest Northstar file in Google Drive and give me only its file "
        "name, MIME type, owner, and modified time. Do not open or read its contents, "
        "and do not change anything."
    )
    plan, names = _workspace_plan_and_tools(prompt)

    contract = cli._context_agent_tool_execution_contract(
        "google_workspace_context_agent",
        input_text=prompt,
        selected_tool_names=sorted(names),
        manual_plan=plan,
    )

    assert contract is not None
    required_tools = {
        tool_name for group in contract.required_groups for tool_name in group.any_of_tool_names
    }
    assert _WORKSPACE_CONTENT_READERS.isdisjoint(required_tools)
    assert {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
    } <= required_tools
