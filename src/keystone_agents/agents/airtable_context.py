"""Airtable context specialist agent."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import filter_tools_for_tier
from keystone_agents.finance_expense_receipts import (
    resolve_finance_expense_receipt_target,
)
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.schemas.operational_context import AirtableContextResult
from keystone_agents.sdk import (
    Agent,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
)
from keystone_agents.semantic_execution import ExecutionIntentAuthority
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.tools.internal_data_tools import (
    airtable_aggregate_records,
    airtable_create_expense_from_receipt,
    airtable_delete_test_record,
    airtable_get_base_schema,
    airtable_link_attachment,
    airtable_read_records,
    airtable_reconcile_duplicate_expense,
    airtable_test_record_lifecycle,
    airtable_upload_attachment,
    airtable_write_record,
)


def _airtable_context_tools(
    *,
    tool_tier: str | int | None = None,
    request_text: str = "",
    manual_plan: object | None = None,
) -> list[Any]:
    tools: list[Any] = [
        airtable_get_base_schema,
        airtable_read_records,
        airtable_aggregate_records,
        airtable_reconcile_duplicate_expense,
        airtable_write_record,
        airtable_link_attachment,
        airtable_upload_attachment,
        airtable_create_expense_from_receipt,
        airtable_delete_test_record,
        airtable_test_record_lifecycle,
    ]
    if tool_tier is not None:
        tools = filter_tools_for_tier("airtable_context_agent", tools, tool_tier)
    normalized = " ".join(str(request_text or "").lower().split())
    authority = ExecutionIntentAuthority.from_value(manual_plan)
    if authority.invalid:
        return []
    plan = authority.plan if authority.canonical else None
    if plan is not None and plan.provider_system != "airtable":
        return []
    provider_operations = (
        set(authority.effective_provider_operations("airtable"))
        if plan is not None
        else set()
    )
    marked_lifecycle = bool(
        re.search(r"\bkba_test_record(?:_[a-z0-9]+)*\b", normalized)
        and (
            {"create", "update", "delete"} <= provider_operations
            if plan is not None
            else (
                re.search(r"\b(?:add|create|make|write|insert)\b", normalized)
                and re.search(r"\b(?:update|change|modify|revise|edit|set)\b", normalized)
                and re.search(r"\b(?:delete|remove|clean\s*up)\b", normalized)
            )
        )
    )
    if marked_lifecycle:
        return [
            tool
            for tool in tools
            if getattr(tool, "name", "") == "airtable_test_record_lifecycle"
        ]
    receipt_target = resolve_finance_expense_receipt_target(
        request_text,
        manual_plan=manual_plan,
    )
    if receipt_target is not None and receipt_target.operation == "create":
        # This composite tool owns artifact reading, live schema mapping, the
        # single record create, attachment upload, and provider read-back. Do
        # not ask the model to assemble that lifecycle from unrelated tools.
        return [
            tool
            for tool in tools
            if getattr(tool, "name", "") == "airtable_create_expense_from_receipt"
        ]
    if receipt_target is not None and receipt_target.operation == "update":
        # Receipt context may be mentioned only to identify an existing expense.
        # Keep the generic schema/read/update tools available and remove every
        # create-oriented tool so an in-place correction cannot create a row.
        allowed = {
            "airtable_get_base_schema",
            "airtable_read_records",
            "airtable_write_record",
            "airtable_reconcile_duplicate_expense",
        }
        return [tool for tool in tools if getattr(tool, "name", "") in allowed]
    if receipt_target is not None and receipt_target.operation == "read":
        return [
            tool
            for tool in tools
            if getattr(tool, "name", "")
            in {"airtable_get_base_schema", "airtable_read_records"}
        ]
    if plan is not None:
        allowed = {"airtable_get_base_schema"}
        if provider_operations.intersection({"read", "search", "verify"}):
            allowed.update({"airtable_read_records", "airtable_aggregate_records"})
        if provider_operations.intersection({"create", "update"}):
            allowed.add("airtable_write_record")
        if "attach" in provider_operations:
            allowed.update(
                {
                    "airtable_link_attachment",
                    "airtable_upload_attachment",
                }
            )
        return [tool for tool in tools if getattr(tool, "name", "") in allowed]
    return tools


def build_airtable_context_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    manual_plan: object | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: str | int | None = None,
    attach_tools: bool = True,
    compact_instructions: bool = False,
) -> Agent:
    """Build the Airtable context specialist."""

    skill_files = select_agent_skill_names(
        "airtable_context_agent",
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
        compact=compact_instructions,
    )
    composer = compose_direct_instructions if compact_instructions else compose_instructions
    prompt_files = (
        ("keystone_profile.md", "safety_policy.md", "airtable_context.md")
        if compact_instructions
        else ("keystone_profile.md", "safety_policy.md", "tools.md", "airtable_context.md")
    )
    instructions = composer(*prompt_files, skill_files=skill_files)
    return build_sdk_agent(
        name="airtable_context_agent",
        instructions=instructions,
        output_type=AirtableContextResult,
        tools=(
            _airtable_context_tools(
                tool_tier=tool_tier,
                request_text=request_text,
                manual_plan=manual_plan,
            )
            if attach_tools
            else []
        ),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="airtable_context_agent",
        handoff_description=(
            "Use for Airtable base, table, field, and candidate record context, "
            "plus direct approved create/update writes when invoked as the selected agent."
        ),
    )
