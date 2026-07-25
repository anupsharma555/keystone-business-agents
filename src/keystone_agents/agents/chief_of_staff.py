"""KNI Chief of Staff agent builder and deterministic routing fallback."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from keystone_agents.agent_tool_policy import (
    AIRTABLE_WRITE_ALLOWED_TOOLS,
    CALENDAR_WRITE_TOOL_NAMES,
    GOOGLE_WORKSPACE_READ_TOOLS,
    GOOGLE_WORKSPACE_WRITE_TOOLS,
    INTERNAL_WRITE_TOOL_NAMES,
    PUBLISH_TOOL_NAMES,
    tool_name_for_policy,
)
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.automation_inventory import build_automation_inventory_report
from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.config import parse_bool
from keystone_agents.file_search import append_configured_file_search_tools
from keystone_agents.finance_expense_receipts import (
    extract_finance_receipt_evidence,
    finance_expense_receipt_provider_context,
    infer_finance_expense_receipt_target,
    match_receipt_evidence_to_airtable_fields,
    resolve_finance_expense_receipt_target,
)
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.local_kni_evidence import (
    build_local_kni_evidence_packet_for_query,
    looks_like_local_kni_evidence_lookup,
)
from keystone_agents.memory import (
    build_chief_of_staff_memory_context,
    chief_of_staff_memory_item,
    operator_reference_memory_item,
)
from keystone_agents.model_provider import get_runtime_agent_model
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.planning.compatibility import (
    infer_manual_request_plan,
    looks_like_supplied_context_synthesis_request,
)
from keystone_agents.quality_budget import (
    AgentQualityBudget,
    QualityMode,
    chief_of_staff_quality_budget,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.automation import (
    AutomationArtifactRef,
    AutomationWriteDestination,
    ChiefOfStaffWriteRequest,
)
from keystone_agents.schemas.chief_of_staff import (
    ChiefContextHandoff,
    ChiefContextHandoffAgent,
    ChiefDurableHandoff,
    ChiefDurableHandoffAgent,
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
    ChiefSlackCommandResolution,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import (
    Agent,
    build_model_settings,
    build_sdk_agent,
    compose_direct_instructions,
    compose_instructions,
    load_prompt,
)
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.specialist_agent_tools import (
    SpecialistToolMode,
    build_specialist_agent_tools,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.automation_inventory_tool import (
    inspect_active_work_items,
    list_automation_specs,
    list_channel_automation_bindings,
    list_pending_automation_approvals,
    list_recent_automation_runs,
    summarize_automation_health,
)
from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics,
    summarize_rendered_page_diagnostics,
)
from keystone_agents.tools.chief_of_staff_tool import (
    OFFICIAL_OPERATIONS_DOCS,
    _capability_for_topic,
    list_chief_of_staff_context_sources,
    list_slack_slash_commands,
    lookup_slack_workflow_capability,
    read_slack_repo_context_file,
    search_official_operations_docs,
    search_slack_repo_context,
    summarize_slack_runtime_config,
    validate_slack_slash_command,
)
from keystone_agents.tools.google_calendar_tool import (
    create_google_calendar_event,
    delete_google_calendar_event,
    read_google_calendar_window,
    update_google_calendar_event,
)
from keystone_agents.tools.html_review_tool import extract_research_claims_from_html
from keystone_agents.tools.internal_data_tools import (
    airtable_create_expense_from_receipt,
    airtable_get_base_schema,
    airtable_get_base_schema_impl,
    airtable_read_records,
    airtable_read_records_impl,
    airtable_upload_attachment,
    airtable_write_record,
    airtable_write_record_impl,
    explicit_full_article_read_requested,
    google_doc_read,
    google_drive_get_file_metadata,
    google_drive_list_folder,
    google_drive_search_files,
    google_sheet_list,
    google_sheet_read_table,
    google_workspace_tools,
    read_linked_article,
)
from keystone_agents.tools.kni_document_tool import (
    list_kni_document_folder,
    list_kni_document_sources,
    read_kni_document_file,
    search_kni_documents,
)
from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.memory_tool import retrieve_chief_of_staff_memory
from keystone_agents.tools.operations_publisher_tool import (
    publish_document_report,
    publish_internal_artifact,
    publish_slack_summary,
    publish_table_mirror,
)
from keystone_agents.tools.playwright_tool import render_page
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema

CHIEF_OF_STAFF_REASONING_EFFORT = "low"
CHIEF_OF_STAFF_VERBOSITY = "low"
CHIEF_OF_STAFF_MAX_TOKENS = 2_500
CHIEF_OF_STAFF_SPECIALIST_TOOLS_ENV = "KEYSTONE_CHIEF_OF_STAFF_SPECIALIST_TOOLS"


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_specialist_tool_mode(value: str | SpecialistToolMode) -> SpecialistToolMode:
    normalized = str(value or "").strip().lower()
    if normalized in {"read_plan", "approved_write"}:
        return normalized  # type: ignore[return-value]
    raise ValueError(
        "Unsupported Chief of Staff specialist tool mode "
        f"{value!r}; expected read_plan or approved_write."
    )


def chief_of_staff_should_use_specialist_tools(
    request_text: str,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> bool:
    """Return true when Chief of Staff should consult specialist agents as tools."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    plan = authority.plan
    if authority.canonical and plan is not None:
        specialist_routes = {
            "gmail_triage",
            "business_research_analyst",
            "opportunity_scout",
            "outreach_composer",
            "airtable_context_agent",
            "google_workspace_context_agent",
            "zotero_context_agent",
            "rss_context_agent",
            "preprints_context_agent",
        }
        return bool(
            plan.target_agent in specialist_routes
            or any(route in specialist_routes for route in plan.workflow)
        )
    if authority.invalid:
        return False
    text = _chief_positive_specialist_request_text(request_text)
    if plan is not None and plan.intent in {
        "company_research",
        "research_brief",
        "opportunity_search",
        "opportunity_to_outreach_loop",
        "gmail_triage",
        "outreach_draft",
        "browser_diagnostics",
        "context_lookup",
    }:
        return _chief_text_has_positive_specialist_marker(text)
    if plan is not None and plan.task_objective in {
        "entity_research",
        "source_research",
        "opportunity_discovery",
        "contact_discovery",
        "outreach_draft",
        "gmail_triage",
        "browser_diagnostics",
        "context_lookup",
    }:
        return _chief_text_has_positive_specialist_marker(text)
    return _chief_text_has_positive_specialist_marker(text)


def _chief_specialist_routes_from_plan(
    manual_request_plan: ManualRequestPlan | None,
) -> set[str] | None:
    """Return exact canonical specialist owners, or ``None`` for compatibility."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if not authority.canonical:
        return None
    assert authority.plan is not None
    specialist_routes = {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }
    requested = {authority.plan.target_agent, *authority.plan.workflow}
    return requested.intersection(specialist_routes)


def _chief_positive_specialist_request_text(request_text: str) -> str:
    text = str(request_text or "").lower()
    text = re.sub(
        r"\b(?:do not|don't|dont|never|without|no)\b[^.\n;:]{0,220}"
        r"\b(?:gmail|inbox|email|e-mail|thread|outreach|draft|reply|respond|"
        r"airtable|google drive|google doc|google sheet|workspace|zotero|live web|"
        r"research externally|external(?:ly)?|crm|publish|post|schedule|send)\b"
        r"[^.\n;:]*",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:sanitized|provided|inline)\b[^.\n;]{0,120}"
        r"\b(?:gmail triage|email triage|outreach|airtable|google workspace|"
        r"google drive|zotero)\b[^.\n;]{0,120}\b(?:diagnostic|context|output|result)\b",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:from|based on)\b[^.\n;]{0,80}"
        r"\b(?:gmail triage|email triage|outreach|airtable|google workspace|"
        r"google drive|zotero)\b[^.\n;]{0,80}\b(?:diagnostic|context|output|result)\b",
        " ",
        text,
        flags=re.I,
    )
    return " ".join(text.split())


def _chief_text_has_positive_specialist_marker(text: str) -> bool:
    cross_agent_markers = (
        "business research",
        "research analyst",
        "research brief",
        "source-backed",
        "deeper search",
        "find companies",
        "company research",
        "opportunity scout",
        "opportunity search",
        "gmail",
        "email thread",
        "inbox",
        "outreach",
        "draft email",
        "draft reply",
        "outreach follow-up",
        "outreach follow up",
        "email follow-up",
        "email follow up",
        "airtable",
        "base schema",
        "record identity",
        "field mapping",
        "google drive",
        "google doc",
        "google sheet",
        "drive folder",
        "workspace folder",
        "spreadsheet",
        "kniops",
        "zotero",
        "zotero collection",
        "zotero article",
        "zotero library",
        "literature collection",
        "paper collection",
        "article collection",
        "rss context",
        "feed context",
        "announcement history",
        "announcements context",
        "preprints context",
        "preprint context",
        "preprint",
        "preprints",
        "compare agents",
        "across agents",
        "multiple agents",
    )
    if any(marker in text for marker in cross_agent_markers):
        return True
    if re.search(
        r"\b(?:grants?|rfps?)\b"
        r"(?:\s+(?:opportunity|program|funding|application|deadline|search))?\b"
        r"(?!\s+approval\b)",
        text,
    ):
        return True
    next_action_markers = ("what should we do next", "next best action", "prioritize")
    operating_surfaces = ("research", "opportunit", "outreach", "gmail", "email", "company")
    return any(marker in text for marker in next_action_markers) and any(
        surface in text for surface in operating_surfaces
    )


def _chief_of_staff_tools(
    request_text: str = "",
    *,
    manual_request_plan: ManualRequestPlan | None = None,
    specialist_tools: list[Any] | None = None,
) -> list[Any]:
    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if authority.invalid:
        return []
    if authority.canonical:
        assert authority.plan is not None
        return _canonical_chief_of_staff_tools(
            request_text,
            authority,
            specialist_tools=specialist_tools,
        )
    if (
        manual_request_plan is not None
        and manual_request_plan.provider_system == "google_calendar"
    ):
        calendar_tools = [read_google_calendar_window]
        if manual_request_plan.intent == "business_system_write":
            operation_tools = {
                "create": create_google_calendar_event,
                "update": update_google_calendar_event,
                "delete": delete_google_calendar_event,
            }
            selected_operations = (
                manual_request_plan.provider_operations
                if ExecutionIntentAuthority.from_value(manual_request_plan).canonical
                and manual_request_plan.provider_operations
                else list(operation_tools)
            )
            calendar_tools.extend(
                operation_tools[operation]
                for operation in selected_operations
                if operation in operation_tools
            )
        return calendar_tools
    if _chief_plan_requests_local_kni_documents(
        manual_request_plan,
        request_text=request_text,
    ):
        return [
            list_kni_document_folder,
            list_kni_document_sources,
            search_kni_documents,
            read_kni_document_file,
        ]

    tools = [
        list_chief_of_staff_context_sources,
        list_slack_slash_commands,
        summarize_slack_runtime_config,
        search_slack_repo_context,
        read_slack_repo_context_file,
        lookup_slack_workflow_capability,
        validate_slack_slash_command,
        search_official_operations_docs,
        retrieve_chief_of_staff_memory,
        list_automation_specs,
        list_recent_automation_runs,
        list_channel_automation_bindings,
        summarize_automation_health,
        list_pending_automation_approvals,
        inspect_active_work_items,
        publish_document_report,
        publish_table_mirror,
        publish_slack_summary,
        list_kni_document_folder,
        list_kni_document_sources,
        search_kni_documents,
        read_kni_document_file,
        list_local_context_sources,
        search_local_context,
        read_local_context_file,
        extract_research_claims_from_html,
        structure_web_data_for_schema,
        search_web,
        render_page,
        capture_browser_diagnostics,
        summarize_rendered_page_diagnostics,
        publish_internal_artifact,
        airtable_get_base_schema,
        airtable_read_records,
        airtable_write_record,
        airtable_upload_attachment,
        airtable_create_expense_from_receipt,
        create_google_calendar_event,
        update_google_calendar_event,
        delete_google_calendar_event,
        read_google_calendar_window,
        *(specialist_tools or []),
        google_doc_read,
        google_drive_list_folder,
        google_drive_search_files,
        google_drive_get_file_metadata,
        google_sheet_list,
        google_sheet_read_table,
        *google_workspace_tools(),
    ]
    if explicit_full_article_read_requested(request_text):
        tools.append(read_linked_article)
    tools = _scope_chief_write_tools(
        tools,
        request_text=request_text,
        manual_request_plan=manual_request_plan,
    )
    return append_configured_file_search_tools(
        "chief_of_staff",
        tools,
    )


def _canonical_chief_of_staff_tools(
    request_text: str,
    authority: ExecutionIntentAuthority,
    *,
    specialist_tools: list[Any] | None,
) -> list[Any]:
    """Compile one bounded Chief toolbox from the canonical plan."""

    assert authority.plan is not None
    plan = authority.plan
    tools: list[Any] = list(specialist_tools or [])
    provider = plan.provider_system
    operations = set(authority.effective_provider_operations(provider))

    if provider == "google_calendar":
        tools.append(read_google_calendar_window)
        operation_tools = {
            "create": create_google_calendar_event,
            "update": update_google_calendar_event,
            "delete": delete_google_calendar_event,
        }
        tools.extend(
            tool for operation, tool in operation_tools.items() if operation in operations
        )
        return _unique_tools(tools)

    if (
        plan.target_agent == "chief_of_staff"
        and plan.intent == "context_lookup"
        and plan.target_type == "local_document_collection"
    ):
        return [
            list_kni_document_folder,
            list_kni_document_sources,
            search_kni_documents,
            read_kni_document_file,
        ]

    if provider == "airtable":
        tools.append(airtable_get_base_schema)
        if operations.intersection({"read", "search", "verify", "create", "update", "attach"}):
            tools.append(airtable_read_records)
        receipt_target = resolve_finance_expense_receipt_target(
            request_text,
            manual_plan=plan,
        )
        if receipt_target is not None and operations.intersection({"create", "attach"}):
            tools.append(airtable_create_expense_from_receipt)
        elif operations.intersection({"create", "update"}):
            tools.append(airtable_write_record)
        if "attach" in operations and receipt_target is None:
            tools.append(airtable_upload_attachment)
        return _unique_tools(tools)

    if provider == "google_workspace":
        workspace_tools = google_workspace_tools()
        include_names = set(GOOGLE_WORKSPACE_READ_TOOLS)
        if operations.intersection({"create", "update", "delete", "attach"}):
            include_names.update(GOOGLE_WORKSPACE_WRITE_TOOLS)
        tools.extend(
            tool
            for tool in workspace_tools
            if tool_name_for_policy(tool) in include_names
        )
        return _unique_tools(tools)

    if provider == "slack" or plan.intent == "slack_operations":
        tools.extend(
            [
                list_slack_slash_commands,
                summarize_slack_runtime_config,
                search_slack_repo_context,
                read_slack_repo_context_file,
                lookup_slack_workflow_capability,
                validate_slack_slash_command,
            ]
        )
        return _unique_tools(tools)

    if plan.intent == "browser_diagnostics":
        tools.extend(
            [
                render_page,
                capture_browser_diagnostics,
                summarize_rendered_page_diagnostics,
            ]
        )
        return _unique_tools(tools)

    if plan.intent in {"company_research", "research_brief"} and not tools:
        tools.extend(
            [
                search_web,
                extract_research_claims_from_html,
                structure_web_data_for_schema,
            ]
        )
        if explicit_full_article_read_requested(request_text):
            tools.append(read_linked_article)

    if plan.intent == "continue_work_item" or plan.requires_durable_state:
        tools.append(inspect_active_work_items)
    if plan.target_type == "operator_reference":
        tools.append(retrieve_chief_of_staff_memory)
    return _unique_tools(tools)


def _unique_tools(tools: list[Any]) -> list[Any]:
    selected: list[Any] = []
    seen: set[str] = set()
    for tool in tools:
        name = tool_name_for_policy(tool)
        if not name or name in seen:
            continue
        seen.add(name)
        selected.append(tool)
    return selected


def _scope_chief_write_tools(
    tools: list[Any],
    *,
    request_text: str,
    manual_request_plan: ManualRequestPlan | None,
) -> list[Any]:
    """Attach only writes owned by the interpreted structured-system request."""

    if manual_request_plan is None:
        return tools
    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    effective_operations = set(
        authority.effective_provider_operations(manual_request_plan.provider_system)
    )
    allowed_writes: set[str] = set()
    if (
        manual_request_plan.provider_system == "google_calendar"
        and manual_request_plan.intent == "business_system_write"
    ):
        if authority.canonical:
            calendar_write_names = {
                "create": "create_google_calendar_event",
                "update": "update_google_calendar_event",
                "delete": "delete_google_calendar_event",
            }
            allowed_writes.update(
                calendar_write_names[operation]
                for operation in effective_operations
                if operation in calendar_write_names
            )
        else:
            allowed_writes.update(CALENDAR_WRITE_TOOL_NAMES)
    elif manual_request_plan.intent == "business_system_write":
        if manual_request_plan.provider_system == "airtable":
            receipt_target = resolve_finance_expense_receipt_target(
                request_text,
                manual_plan=manual_request_plan,
            )
            if receipt_target is not None and receipt_target.operation == "create":
                allowed_writes.add("airtable_create_expense_from_receipt")
            elif receipt_target is not None and receipt_target.operation == "update":
                allowed_writes.update(
                    {
                        "airtable_write_record",
                        "airtable_reconcile_duplicate_expense",
                    }
                )
            elif authority.canonical:
                if "create" in effective_operations or "update" in effective_operations:
                    allowed_writes.add("airtable_write_record")
                if "attach" in effective_operations:
                    allowed_writes.update(
                        {"airtable_upload_attachment", "airtable_link_attachment"}
                    )
            else:
                allowed_writes.update(AIRTABLE_WRITE_ALLOWED_TOOLS)
        elif manual_request_plan.provider_system == "google_workspace":
            if (
                not authority.canonical
                or effective_operations.intersection(
                    {"create", "update", "delete", "attach"}
                )
            ):
                allowed_writes.update(GOOGLE_WORKSPACE_WRITE_TOOLS)
    blocked_writes = INTERNAL_WRITE_TOOL_NAMES | PUBLISH_TOOL_NAMES
    return [
        tool
        for tool in tools
        if (
            (name := tool_name_for_policy(tool)) not in blocked_writes
            or name in allowed_writes
        )
    ]


def build_chief_slack_command_resolver_agent(
    *, model: str | None = None
) -> Agent:
    """Build the compact Chief mode that only selects a native KS command."""

    return build_sdk_agent(
        name="chief_slack_command_resolver",
        instructions=load_prompt("chief_slack_command_resolver.md").strip(),
        output_type=ChiefSlackCommandResolution,
        tools=[],
        model=get_runtime_agent_model("chief_of_staff", model_override=model),
        model_settings=build_model_settings(
            reasoning_effort="low",
            verbosity="low",
            max_tokens=300,
        ),
        handoff_description="Resolve one natural-language Chief ask to one native KS command.",
    )


def run_chief_slack_command_resolver(
    request_text: str,
    command_catalog: list[dict[str, str]],
    *,
    live: bool = False,
    model: str | None = None,
    run_config: Any | None = None,
) -> TypedAgentRunResult[ChiefSlackCommandResolution]:
    """Resolve a native Slack command with one compact model turn and no tools."""

    return run_typed_sdk_agent(
        agent=build_chief_slack_command_resolver_agent(model=model),
        typed_input={
            "request": " ".join(str(request_text or "").split()),
            "configured_commands": command_catalog,
            "execution_boundary": (
                "Selection only. Keystone Slack WorkflowRunner executes the result."
            ),
        },
        output_type=ChiefSlackCommandResolution,
        run_config=run_config,
        live=live,
        workflow_name="chief_slack_command_resolution",
        trace_metadata={
            "agent_name": "chief_of_staff",
            "run_kind": "native_slack_command_resolution",
        },
        max_turns=1,
    )


def validate_chief_slack_command_resolution(
    resolution: ChiefSlackCommandResolution,
    command_catalog: list[dict[str, str]],
) -> ChiefSlackCommandResolution:
    """Reject model-invented commands before KS backend dispatch."""

    if resolution.status != "matched":
        return resolution
    configured = {
        str(item.get("command") or "").strip()
        for item in command_catalog
        if str(item.get("command") or "").strip()
    }
    command_name = resolution.command_text.split(" ", 1)[0].strip().lower()
    if command_name in configured:
        return resolution
    return ChiefSlackCommandResolution(
        status="no_match",
        rationale="The selected command is not configured in the Keystone Slack manifest.",
        confidence="low",
    )


_SLACK_COMMAND_MATCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "can",
        "chief",
        "do",
        "for",
        "get",
        "kni",
        "me",
        "of",
        "on",
        "please",
        "run",
        "slack",
        "staff",
        "the",
        "to",
        "with",
    }
)


def _command_match_token(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "", value.lower())
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def _command_match_tokens(value: str) -> set[str]:
    return {
        normalized
        for raw in re.findall(r"[A-Za-z0-9]+", value)
        if (normalized := _command_match_token(raw))
        and normalized not in _SLACK_COMMAND_MATCH_STOPWORDS
    }


def _deterministic_command_arguments(
    request_text: str,
    command_tokens: set[str],
) -> str:
    text = re.sub(
        r"^\s*(?:slack\s+)?chief\s+of\s+staff\s*[:,;\-]*\s*",
        "",
        " ".join(str(request_text or "").split()),
        flags=re.I,
    )
    kept: list[str] = []
    for raw in text.split():
        normalized = _command_match_token(raw)
        if normalized in _SLACK_COMMAND_MATCH_STOPWORDS or normalized in command_tokens:
            continue
        kept.append(raw.strip(" ,;:"))
    return " ".join(item for item in kept if item)


def resolve_high_confidence_chief_slack_command(
    request_text: str,
    command_catalog: list[dict[str, str]],
) -> ChiefSlackCommandResolution | None:
    """Resolve an unambiguous manifest command without spending a model call."""

    request_tokens = _command_match_tokens(request_text)
    scored: list[tuple[int, int, str, set[str]]] = []
    for item in command_catalog:
        command = str(item.get("command") or "").strip()
        if not command or command == "/kni":
            continue
        command_tokens = _command_match_tokens(command.removeprefix("/kni-"))
        semantic_tokens = _command_match_tokens(
            " ".join(
                (
                    str(item.get("description") or ""),
                    str(item.get("usage_hint") or ""),
                )
            )
        )
        command_overlap = len(request_tokens & command_tokens)
        semantic_overlap = len(request_tokens & semantic_tokens)
        score = command_overlap * 4 + semantic_overlap
        if score:
            scored.append((score, len(command_tokens), command, command_tokens))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    if not scored:
        return None
    top = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0
    if top[0] < 4 or top[0] - runner_up_score < 2:
        return None
    arguments = _deterministic_command_arguments(request_text, top[3])
    command_text = " ".join(part for part in (top[2], arguments) if part)
    return ChiefSlackCommandResolution(
        status="matched",
        command_text=command_text,
        rationale="Unique high-confidence match from the configured Slack command catalog.",
        confidence="high",
    )


def chief_slack_command_resolution_is_applicable(request_text: str) -> bool:
    """Keep native-command resolution from intercepting provider-owned actions."""

    text = " ".join(str(request_text or "").split()).strip()
    if not text:
        return False
    if infer_calendar_action_plan(text) is not None:
        return False
    plan = infer_manual_request_plan(text, requested_agent="chief_of_staff")
    if plan.intent == "business_system_write":
        return False
    if plan.target_agent in {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "gmail_triage",
    }:
        return False
    return True


def _local_kni_document_context_requested(request_text: str) -> bool:
    return looks_like_local_kni_evidence_lookup(request_text)


def _chief_plan_requests_local_kni_documents(
    manual_request_plan: ManualRequestPlan | None,
    *,
    request_text: str,
) -> bool:
    """Use the LLM's typed local-document target; retain an offline fallback."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if authority.canonical and manual_request_plan is not None:
        return bool(
            manual_request_plan.target_agent == "chief_of_staff"
            and manual_request_plan.intent == "context_lookup"
            and manual_request_plan.target_type == "local_document_collection"
            and manual_request_plan.provider_system == "unspecified"
        )
    if authority.invalid:
        return False
    return _local_kni_document_context_requested(request_text)


def _looks_like_local_kni_document_lookup_request(request_text: str) -> bool:
    lowered = str(request_text or "").lower()
    return _local_kni_document_context_requested(lowered) and any(
        marker in lowered
        for marker in (
            "what date",
            "when",
            "who",
            "which",
            "formed",
            "formation",
            "date filed",
            "certificate of organization",
            "articles of organization",
            "organizer",
            "organized",
            "organised",
            "registered agent",
            "registered office",
            "signer",
            "who filed",
            "filer",
            "formally organized",
            "formally organised",
            "evidence path",
            "using local",
            "search",
            "read",
            "insurance",
            "policy",
            "certificate of insurance",
            "coi",
            "peo",
            "provider",
            "carrier",
            "broker",
            "proposal",
            "capability statement",
            "statement of capabilities",
            "service areas",
            "service offerings",
        )
    )


URL_PATTERN = re.compile(r"https?://[^\s<>)]+", flags=re.I)
SLACK_HISTORY_DIGEST_MARKER = "Slack channel history digest:"
SLACK_HISTORY_ITEM_RE = re.compile(
    r"^-\s+ts=(?P<ts>\S+)\s+author=(?P<author>\S+)"
    r"(?:\s+title=(?P<title>.*?))?:\s+(?P<body>.*)$"
)
SLACK_FOLLOWUP_MARKER_RE = re.compile(
    r"(?:^|\n)\s*(?:User follow-up|Latest operator follow-up):\s*",
    flags=re.I,
)
BLOCKED_SIDE_EFFECTS = [
    "unscoped_slack_post",
    "gmail_send",
    "calendar_create_or_update",
    "repo_write",
    "linkedin_publish",
    "crm_write",
]


def _docs_for_topic(topic: str) -> list[ChiefOfStaffSourceRef]:
    normalized = topic.lower()
    source_type_markers = {
        "openai_docs": (
            "openai",
            "agents sdk",
            "agent sdk",
            "developer docs",
            "orchestration",
            "handoff",
            "guardrail",
        ),
        "slack_docs": (
            "slack",
            "socket mode",
            "slash command",
            "chat.postmessage",
            "channel",
        ),
        "airtable_docs": ("airtable",),
        "irs_docs": ("irs", "federal tax", "estimated tax", "self-employed"),
        "pa_revenue_docs": ("pennsylvania", " pa tax", "state tax"),
        "philadelphia_revenue_docs": (
            "philadelphia",
            "birt",
            "net profits tax",
            "school income tax",
            "city tax",
        ),
    }
    selected = []
    for doc in OFFICIAL_OPERATIONS_DOCS:
        markers = source_type_markers.get(doc.source_type, ())
        if not markers or not any(marker in normalized for marker in markers):
            continue
        if any(keyword in normalized for keyword in doc.keywords):
            selected.append(doc)
    return [
        ChiefOfStaffSourceRef(
            title=doc.title,
            url=doc.url,
            source_type=doc.source_type,
            note=doc.note,
        )
        for doc in selected[:4]
    ]


FINANCE_TRACKER_TABLES = (
    "Business Income",
    "Business Expenses",
    "Personal Income",
    "Personal Expenses",
    "Tax Payments",
)
FINANCE_TRACKER_DEFAULT_YEAR = 2026
FINANCE_TRACKER_TAX_TABLE_CANDIDATES = (
    "Tax Payments",
    "Tax Expenses",
    "Tax Expense",
)


@dataclass(frozen=True)
class AirtableTableView:
    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class NormalizedFinanceRecord:
    table_name: str
    record_id: str
    fields: Mapping[str, Any]
    period: int | None
    amount: Decimal
    amount_fields: tuple[str, ...]


def _coerce_manual_request_plan(
    value: ManualRequestPlan | Mapping[str, Any] | None,
) -> ManualRequestPlan | None:
    if value is None:
        return None
    if isinstance(value, ManualRequestPlan):
        return value
    try:
        return ManualRequestPlan.model_validate(value)
    except (TypeError, ValueError):
        return None


def _looks_like_web_search_brief_request(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered.strip():
        return False
    has_research_shape = any(
        marker in lowered
        for marker in (
            "deeper search",
            "deepened search",
            "live search",
            "web search",
            "source url",
            "source urls",
            "source-backed",
            "readable brief",
            "research brief",
            "synthesis with source",
            "what is ",
            "what are ",
            "what's ",
        )
    )
    has_brief_or_synthesis = any(
        marker in lowered
        for marker in (
            "brief",
            "synthesis",
            "synthesize",
            "source urls",
            "source url",
            "search providers",
            "metadata section",
        )
    )
    return has_research_shape and has_brief_or_synthesis


def _chief_request_plan(
    request_text: str,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> ManualRequestPlan:
    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if authority.canonical:
        assert authority.plan is not None
        return authority.plan
    if authority.invalid:
        return ManualRequestPlan(
            source="invalid_supplied_plan",
            requested_agent="chief_of_staff",
            target_agent="clarification",
            intent="clarification",
            task_objective="clarification",
            missing_required_information=["valid canonical manual request plan"],
            rationale=(
                "A supplied execution plan was invalid. Chief of Staff did not "
                "reinterpret the raw request through compatibility heuristics."
            ),
            planner_warnings=[
                "Repair or regenerate the canonical plan before execution."
            ],
        )
    plan = authority.plan or infer_manual_request_plan(
        request_text,
        requested_agent="chief_of_staff",
    )
    if _looks_like_web_search_brief_request(request_text):
        constraints = list(plan.constraints)
        for constraint in (
            "source-backed web research",
            "visible source URLs",
            "provider metadata at end",
        ):
            if constraint not in constraints:
                constraints.append(constraint)
        warnings = list(plan.planner_warnings)
        warning = (
            "Chief of Staff request contains web-search brief wording; preserve live "
            "source retrieval and do not collapse to a Slack scope explanation."
        )
        if warning not in warnings:
            warnings.append(warning)
        return plan.model_copy(
            update={
                "intent": "research_brief",
                "target_type": "topic"
                if plan.target_type in {"unknown", "slack_channel"}
                else plan.target_type,
                "task_objective": "source_research",
                "expected_artifact_type": "research_brief",
                "requires_live_search": True,
                "constraints": constraints,
                "planner_warnings": warnings,
            }
        )
    return plan


def _latest_slack_followup_request(request_text: str) -> str:
    """Extract the newest operator ask from a Slack continuation wrapper."""

    text = str(request_text or "")
    matches = list(SLACK_FOLLOWUP_MARKER_RE.finditer(text))
    if not matches:
        return ""
    start = matches[-1].end()
    tail = text[start:].strip()
    if not tail:
        return ""
    stop_markers = (
        "\nPrevious request:",
        "\nPrevious result:",
        "\nLinked WorkItem:",
        "\nContinue the same agent task",
        "\nRead-only Slack",
        "\nSlack thread context",
    )
    stop = len(tail)
    lowered_tail = tail.lower()
    for marker in stop_markers:
        idx = lowered_tail.find(marker.lower())
        if idx >= 0:
            stop = min(stop, idx)
    return tail[:stop].strip().strip('"')


def _chief_of_staff_sdk_input_for_request(
    typed_input: str | Mapping[str, Any],
    *,
    raw_request_text: str,
    manual_request_plan: ManualRequestPlan | None = None,
) -> str | dict[str, Any]:
    """Make Slack follow-up intent explicit before rendering the SDK prompt."""

    latest_request = _latest_slack_followup_request(raw_request_text)
    active_request = latest_request or str(raw_request_text or "")
    if not latest_request and not _looks_like_operator_supplied_synthesis_request(active_request):
        return _with_finance_expense_receipt_context(
            typed_input,
            active_request,
            manual_request_plan=manual_request_plan,
        )

    data: dict[str, Any]
    if isinstance(typed_input, Mapping):
        data = dict(typed_input)
    else:
        data = {"request": str(typed_input or "")}
    data["request"] = active_request
    if latest_request:
        data["latest_operator_request"] = latest_request
        data["raw_slack_thread_request"] = raw_request_text
        data["request_priority_instruction"] = (
            "Answer latest_operator_request directly. Use raw_slack_thread_request, "
            "previous requests, prior results, and Slack thread context only as background. "
            "Do not repeat stale prior-run conclusions unless they are necessary to answer "
            "the latest operator request, and label Slack-observed state separately from "
            "verified current repo/runtime state."
        )
    if _looks_like_operator_supplied_synthesis_request(active_request):
        data["direct_supplied_context_instruction"] = (
            "This is a complete, provider-free direct-answer request over facts supplied "
            "by the operator. Answer from those facts in the requested shape. Do not call "
            "tools, search, select a Slack command, or ask which workflow to use. Put the "
            "human answer in summary; use project-context-review with an empty command and "
            "current-thread target. Keep approval_required and human_review_required true "
            "to satisfy the current Chief output contract, while leaving all sends, posts, "
            "and writes disabled. Leave sources empty unless a source was actually used."
        )
    if re.search(
        r"\b(checklist|ordered|steps?|next\s+\d+|next\s+three)\b",
        active_request,
        re.I,
    ):
        data["response_shape_instruction"] = (
            "The latest request asks for a checklist. Keep summary to one direct "
            "sentence and put the checklist items in recommended_actions in the "
            "requested order. Each action should be specific enough to run or verify."
        )
    return _with_finance_expense_receipt_context(
        data,
        active_request,
        manual_request_plan=manual_request_plan,
    )


def _with_finance_expense_receipt_context(
    typed_input: str | Mapping[str, Any],
    request_text: str,
    *,
    manual_request_plan: ManualRequestPlan | None = None,
) -> str | dict[str, Any]:
    if not _manual_plan_authorizes_finance_expense_receipt(manual_request_plan):
        return typed_input
    target = infer_finance_expense_receipt_target(request_text)
    context = finance_expense_receipt_provider_context(target) if target is not None else []
    if not context:
        return typed_input
    data: dict[str, Any] = (
        dict(typed_input) if isinstance(typed_input, Mapping) else {"request": str(typed_input)}
    )
    existing_context = list(data.get("provider_call_context") or [])
    existing_context.extend(context)
    data["provider_call_context"] = existing_context
    data["finance_expense_receipt_instruction"] = (
        "For this explicit finance_tax_tracker expense receipt request, use "
        'base_alias="finance_tax_tracker" and the target table from '
        "provider_call_context. First call airtable_get_base_schema to resolve exact "
        "field names and attachment-field availability. Read and reason over the "
        "attached receipt PDF/image for vendor, date, amount, total, and estimated-tax "
        "period; do not infer receipt values from the filename alone. Then stage a "
        "reviewable Airtable create-and-attach plan and route approved execution to "
        "Airtable Context or the approved Airtable action handler. Chief of Staff "
        "does not execute Airtable record writes or attachment uploads directly."
    )
    approval_reference = str(data.get("approval_reference") or "").strip()
    data["finance_expense_receipt_tool_plan"] = {
        "handoff_tool_name": "airtable_create_expense_from_receipt",
        "arguments": {
            "local_file_path": target.receipt_local_path if target is not None else "",
            "table": target.table if target is not None else "",
            "base_alias": target.base_alias if target is not None else "finance_tax_tracker",
            "receipt_fields_json": "{}",
            "field_values_json": "{}",
            "approval_reference": approval_reference,
            "live": True,
        },
        "llm_reasoning_required": [
            "Read the receipt content before finalizing values.",
            "Inspect Airtable schema before creating the record.",
            "Pass receipt_fields_json from model-read artifact facts when deterministic parsing is weak.",
            "Pass field_values_json only for exact Airtable field names verified in schema.",
            "Choose optional category/payment_method only when supported by schema and evidence.",
            "Explain any unmapped or review-required fields.",
        ],
        "side_effect_boundary": (
            "Do not execute the create/upload in Chief of Staff. Persist this as a "
            "reviewable handoff plan for Airtable Context or the approved Airtable "
            "action handler; execution still requires live write/upload gates and a "
            "scoped approval_reference."
        ),
    }
    return data


def _finance_expense_receipt_provider_context(request_text: str) -> list[dict[str, str]]:
    target = infer_finance_expense_receipt_target(request_text)
    return finance_expense_receipt_provider_context(target) if target is not None else []


def _finance_expense_receipt_live_preflight_blocker(
    request_text: str,
    *,
    manual_request_plan: ManualRequestPlan | None = None,
) -> ChiefOfStaffResult | None:
    if not _manual_plan_authorizes_finance_expense_receipt(manual_request_plan):
        return None
    if not _finance_expense_receipt_provider_context(request_text):
        return None
    if parse_bool(os.getenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS")):
        return None
    result = _plan_finance_expense_receipt_create_request(
        request_text,
        live=parse_bool(os.getenv("KEYSTONE_AIRTABLE_LIVE_READS")),
    )
    return result.model_copy(
        update={
            "summary": (
                result.summary
                + " Live execution is blocked before the model/tool call because "
                "`AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true` is not set; this avoids "
                "creating an expense record without attaching the receipt."
            ),
            "recommended_actions": [
                (
                    "Enable `AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true` for the approved "
                    "command window if the receipt should be attached."
                ),
                *result.recommended_actions,
            ],
            "audit_notes": [
                "Live SDK preflight blocked a receipt expense create to avoid a partial Airtable write.",
                *result.audit_notes,
            ],
        }
    )


def _manual_plan_authorizes_finance_expense_receipt(
    manual_request_plan: ManualRequestPlan | None,
) -> bool:
    """Use typed authority live; retain the bounded legacy receipt fallback."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if authority.invalid:
        return False
    if authority.fallback_allowed:
        return True
    assert authority.plan is not None
    plan = authority.plan
    operations = set(authority.effective_provider_operations("airtable"))
    return bool(
        plan.provider_system == "airtable"
        and plan.intent == "business_system_write"
        and plan.target_agent in {"chief_of_staff", "airtable_context_agent"}
        and {"create", "attach"}.issubset(operations)
    )


def _manual_plan_allows_finance_tracker_shortcut(
    plan: ManualRequestPlan,
    request_text: str,
) -> bool:
    del request_text
    if plan.target_agent not in {
        "chief_of_staff",
        "orchestrator",
        "airtable_context_agent",
    }:
        return False
    planner_text = " ".join(
        str(part or "")
        for part in (
            plan.primary_target,
            plan.objective,
            " ".join(plan.constraints),
            " ".join(plan.required_entities),
            " ".join(plan.required_terms),
        )
    ).lower()
    has_finance_context = bool(
        re.search(
            r"\b(?:finance|financial|tax|taxes|airtable\s+tracker|finance_tax_tracker|"
            r"business\s+income|business\s+expense|personal\s+income|personal\s+expense|"
            r"tax\s+payments|estimated\s+tax|total\s+expenses|additional\s+taxes|"
            r"amount|q[1-4])\b",
            planner_text,
        )
    )
    return has_finance_context


def _with_manual_plan_audit(
    result: ChiefOfStaffResult,
    plan: ManualRequestPlan,
    *,
    action: str,
) -> ChiefOfStaffResult:
    note = (
        f"Manual request planner ran before Chief of Staff deterministic routing: "
        f"source={plan.source}; target_agent={plan.target_agent}; intent={plan.intent}; "
        f"action={action}."
    )
    return result.model_copy(
        update={"audit_notes": list(dict.fromkeys([note, *result.audit_notes]))}
    )


def _looks_like_finance_tracker_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if _looks_like_wrong_response_diagnostic_request(normalized):
        return False
    markers = (
        "finance_tax_tracker",
        "finance tax tracker",
        "2026 finance",
        "tax tracker",
        "airtable tracker",
        "financial tracker",
        "total expenses",
        "business income",
        "business expense",
        "business expenses",
        "personal income",
        "personal expense",
        "personal expenses",
    )
    if any(marker in normalized for marker in markers):
        return True
    has_finance_metric = any(
        marker in normalized for marker in ("income", "expense", "expenses", "deduction", "spend")
    )
    has_period = bool(re.search(r"\b(?:q[1-4]|quarter\s+[1-4]|20\d{2})\b", normalized))
    has_tax_metric = _looks_like_tax_payment_or_estimate_request(normalized)
    has_rank_metric = _looks_like_top_finance_record_request(normalized)
    if has_tax_metric and not _has_clear_finance_tracker_data_anchor(normalized):
        return False
    return (
        (has_finance_metric or has_tax_metric)
        and has_period
        and (
            _is_aggregate_request(normalized)
            or _is_count_request(normalized)
            or has_rank_metric
            or _looks_like_rolling_tax_summary_request(normalized)
            or has_tax_metric
        )
        or has_rank_metric
    )


def _looks_like_finance_tracker_mutation_request(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not _looks_like_finance_tracker_request(normalized):
        return False
    return any(
        re.search(rf"\b{marker}\b", normalized)
        for marker in (
            "add",
            "attach",
            "create",
            "fill",
            "insert",
            "upload",
            "update",
            "change",
            "correct",
            "adjust",
            "set",
            "modify",
            "edit",
        )
    )


def _looks_like_finance_tracker_artifact_workflow_request(text: str) -> bool:
    normalized = _normalized_text(text)
    if not _looks_like_finance_tracker_request(normalized):
        return False
    has_artifact_surface = any(
        marker in normalized
        for marker in (
            "google doc",
            "google docs",
            "gdrive",
            "google drive",
            "drive folder",
            "folder",
            "doc link",
            "link to the google doc",
        )
    )
    has_artifact_intent = any(
        marker in normalized
        for marker in (
            "create",
            "write",
            "provide a link",
            "provide link",
            "save",
            "artifact",
            "report",
            "analysis",
            "analyze",
            "summary",
        )
    )
    return has_artifact_surface and has_artifact_intent


def _finance_tracker_table_from_text(text: str) -> str:
    normalized = " ".join(str(text or "").lower().split())
    matches = [
        (normalized.index(table.lower()), table)
        for table in FINANCE_TRACKER_TABLES
        if table.lower() in normalized
    ]
    if matches:
        return min(matches, key=lambda match: match[0])[1]
    return ""


def _looks_like_expense_total_sync_request(normalized: str) -> bool:
    has_write_intent = any(
        marker in normalized
        for marker in (
            "sync",
            "synchronize",
            "syncronize",
            "syncronized",
            "synchronized",
            "populate",
            "fill in",
            "backfill",
            "copy",
        )
    )
    has_review_intent = any(
        marker in normalized
        for marker in (
            "review",
            "check",
            "find",
            "identify",
            "missing",
            "blank",
            "empty",
            "not populated",
            "unpopulated",
            "inconsistent",
            "mismatch",
        )
    )
    has_expense_total = "total expenses" in normalized or "total expense" in normalized
    return has_expense_total and (
        (has_write_intent and "amount" in normalized) or has_review_intent
    )


def _finance_tracker_route(command: str) -> ChiefOfStaffRouteRecommendation:
    return ChiefOfStaffRouteRecommendation(
        workflow_type="budget-resource-review",
        command_text=command,
        target_channel="docs",
        rationale=(
            "The request is a scoped finance/tax tracker Airtable operation using "
            "bounded reads or dry-run write planning."
        ),
        requires_live_connector=True,
        requires_human_approval_before_post=True,
    )


def _finance_tracker_result(
    *,
    text: str,
    summary: str,
    actions: list[str],
    command: str,
    audit_notes: list[str] | None = None,
) -> ChiefOfStaffResult:
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text[:500],
        summary=summary,
        target_channels=_extract_target_channels(text, "docs"),
        operating_capabilities=[
            "airtable_schema_read",
            "airtable_capped_record_read",
            "airtable_dry_run_write_plan",
            "finance_tax_tracker_context",
            "tax_review_guardrails",
        ],
        recommended_route=_finance_tracker_route(command),
        recommended_actions=actions,
        blocked_side_effects=[
            *BLOCKED_SIDE_EFFECTS,
            "airtable_delete",
            "airtable_schema_change",
            "tax_filing_or_payment",
            "final_tax_or_legal_advice",
        ],
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        slack_post_policy="requires_human_review",
        sources=_docs_for_topic(f"airtable finance tax irs pennsylvania philadelphia {text}"),
        context_sources_considered=[
            "operator_and_agent_policy",
            "finance_tax_tracker_airtable_schema",
            "finance_tax_tracker_context_doc",
            "official_tax_reference_pack",
        ],
        repo_context_used=_repo_context_for_capability("budget-resource-review"),
        audit_notes=[
            (
                "Deterministic finance/tax tracker handler used to avoid "
                "unnecessary large live-model calls."
            ),
            "No raw credentials were read into the response.",
            *(audit_notes or []),
        ],
    )


def _plan_finance_expense_receipt_create_request(
    text: str,
    *,
    live: bool,
) -> ChiefOfStaffResult:
    context = {item["key"]: item["value"] for item in _finance_expense_receipt_provider_context(text)}
    table = context.get("airtable_target_table") or _finance_tracker_table_from_text(text)
    receipt_path = context.get("receipt_local_path", "")
    receipt_name = Path(receipt_path).name if receipt_path else "operator-supplied receipt"
    file_status = "present" if receipt_path and Path(receipt_path).expanduser().is_file() else "unverified"
    receipt_evidence = extract_finance_receipt_evidence(receipt_path) if receipt_path else None
    evidence_preview = receipt_evidence.supported_field_preview() if receipt_evidence else {}
    evidence_summary = (
        receipt_evidence.summary_fragment()
        if receipt_evidence is not None
        else "Receipt evidence was not read because no local receipt path was detected."
    )
    evidence_read = bool(receipt_evidence and receipt_evidence.content_read)
    schema_mapping: dict[str, Any] = {}
    schema_mapping_status = "not_run"
    if evidence_read:
        try:
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            schema_mapping_status = str(schema.get("status") or "unknown")
            schema_fields = _schema_fields_for_named_table(schema, table)
            if schema_fields:
                schema_mapping = match_receipt_evidence_to_airtable_fields(
                    receipt_evidence,
                    schema_fields,
                )
            elif live:
                schema_mapping_status = "blocked_schema_table_not_found"
        except Exception as exc:  # pragma: no cover - defensive live metadata boundary
            schema_mapping_status = f"blocked_{type(exc).__name__}: {exc}"
    schema_summary = _finance_receipt_schema_mapping_summary(schema_mapping, schema_mapping_status)
    summary = (
        f"Prepare an Airtable expense-create plan for `finance_tax_tracker` / `{table}` "
        f"from `{receipt_name}`. {evidence_summary}{schema_summary} "
        "The live Chief of Staff SDK path should attach the local PDF/image as model "
        "evidence, inspect Airtable schema, map receipt-backed fields, set `Estimated Tax "
        "Periods` from the receipt date, and prefer `airtable_create_expense_from_receipt` "
        "for the approved create-and-attach operation. If extraction was unavailable "
        "or uncertain, do not invent vendor, date, amount, tax period, or attachment "
        "field values."
    )
    write_request = ChiefOfStaffWriteRequest(
        destination=AutomationWriteDestination.AIRTABLE,
        title=f"Create {table} receipt expense",
        summary=(
            "Create one finance_tax_tracker expense record from the operator-supplied "
            "receipt PDF/image, then attach the receipt after record identity is known."
        ),
        approval_required=True,
        live_required=True,
        allowed=False,
        status="planned",
        blocked_reason=(
            "Live execution must use the Chief of Staff SDK/tool path with "
            "AIRTABLE_ALLOW_WRITES=true, AIRTABLE_WRITE_DRY_RUN=false, "
            "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS=true for receipt upload, exact schema "
            "field mapping, and a command approval reference."
        ),
        metadata=json.dumps(
            {
                "base_alias": "finance_tax_tracker",
                "table": table,
                "receipt_local_path": receipt_path,
                "receipt_file_status": file_status,
                "receipt_evidence_read": evidence_read,
                "receipt_evidence_method": (
                    receipt_evidence.extraction_method if receipt_evidence else ""
                ),
                "receipt_evidence_blocker": receipt_evidence.blocker if receipt_evidence else "",
                "receipt_field_preview": evidence_preview,
                "schema_mapping_status": schema_mapping_status,
                "schema_field_mapping": schema_mapping,
                "operation": "create",
                "requires_model_receipt_read": True,
                "requires_schema_field_mapping": True,
                "estimated_tax_period_source": "receipt_date",
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
    )
    return _finance_tracker_result(
        text=text,
        summary=summary,
        command=f"Prepare {table} receipt expense create plan",
        actions=[
            (
                "Review the extracted receipt-backed field preview before live write."
                if evidence_read
                else "Run the live Chief of Staff SDK path so the model can read the attached receipt content."
            ),
            "Use `airtable_get_base_schema` before writing and map only exact schema fields.",
            "Set `Estimated Tax Periods` from the receipt date and the tracker period rules.",
            (
                "Create and attach through `airtable_create_expense_from_receipt`; use "
                "lower-level write/upload tools only if the bounded tool cannot express "
                "the approved operation."
            ),
        ],
        audit_notes=[
            "Explicit finance_tax_tracker receipt-create request bypassed generic context-agent advisory.",
            (
                "Local receipt extraction produced a receipt-backed preview."
                if evidence_read
                else "Deterministic fallback did not infer receipt values from filename or current date."
            ),
            f"Receipt file status: {file_status}.",
            (
                f"Receipt extraction method: {receipt_evidence.extraction_method}."
                if evidence_read and receipt_evidence is not None
                else ""
            ),
        ],
    ).model_copy(
        update={
            "write_requests": [write_request],
            "recommended_route": ChiefOfStaffRouteRecommendation(
                workflow_type="artifact-write-plan",
                command_text=f"Create {table} receipt expense from attached receipt",
                target_channel="docs",
                rationale=(
                    "The request asks Chief of Staff to create a finance tracker expense "
                    "record from local receipt evidence through Airtable tools."
                ),
                requires_live_connector=True,
                requires_human_approval_before_post=True,
            ),
        }
    )


def _plan_finance_tracker_artifact_workflow_request(text: str) -> ChiefOfStaffResult:
    write_requests = _write_requests_from_text(text)
    return _finance_tracker_result(
        text=text,
        summary=(
            "Plan a Q1 finance_tax_tracker tax analysis artifact in Google Drive. "
            "The live run should read the Airtable schema and records, include the "
            "Tax Payments rolling summary note, create the KNIOps folder/doc through "
            "Google Workspace tools, and return the Google Doc link."
        ),
        actions=[
            "Read finance_tax_tracker schema and Q1 records from the finance tables.",
            "Separate paid tax rows from rolling summary/helper rows in Tax Payments.",
            (
                "Create the requested KNIOps Drive folder and Google Doc only through "
                "typed Google Workspace tools."
            ),
            "Return the Google Doc link, or the exact missing live-write configuration if blocked.",
        ],
        command=("Create Q1 finance_tax_tracker tax analysis Google Doc from Airtable reads"),
        audit_notes=[
            "Finance/tax Google Doc request routed as a finance tracker artifact workflow.",
        ],
    ).model_copy(
        update={
            "operating_capabilities": [
                "airtable_schema_read",
                "airtable_capped_record_read",
                "finance_tax_tracker_context",
                "tax_review_guardrails",
                "google_drive_folder_planning",
                "google_doc_artifact_planning",
            ],
            "recommended_route": ChiefOfStaffRouteRecommendation(
                workflow_type="artifact-write-plan",
                command_text=(
                    "@KNI chief of staff create Q1 finance_tax_tracker tax analysis "
                    "Google Doc from Airtable reads"
                ),
                target_channel="docs",
                rationale=(
                    "The request asks for a finance/tax tracker analysis artifact, "
                    "not a generic company or opportunity research artifact."
                ),
                requires_live_connector=True,
                requires_human_approval_before_post=True,
            ),
            "target_channels": _extract_target_channels(text, "docs"),
            "context_sources_considered": [
                "operator_and_agent_policy",
                "finance_tax_tracker_airtable_schema",
                "finance_tax_tracker_records",
                "tax_payments_rolling_summary_notes",
                "google_workspace_write_policy",
            ],
            "write_requests": write_requests,
        }
    )


def _field_names_from_records(records: list[dict[str, Any]]) -> list[str]:
    names: set[str] = set()
    for record in records:
        fields = record.get("fields", {})
        if isinstance(fields, Mapping):
            names.update(str(name) for name in fields)
    return sorted(names)


def _normalized_text(text: object) -> str:
    return " ".join(str(text or "").lower().split())


def _active_slack_followup_text(text: str) -> str:
    raw = str(text or "")
    lowered = raw.lower()
    markers = (
        "user follow-up:",
        "current request:",
        "follow-up request:",
    )
    marker_positions = [
        (lowered.rfind(marker), marker) for marker in markers if lowered.rfind(marker) != -1
    ]
    if not marker_positions:
        return raw
    start, marker = max(marker_positions, key=lambda item: item[0])
    focused = raw[start + len(marker) :].strip()
    return focused or raw


def _schema_table_views(schema_result: Mapping[str, Any]) -> list[AirtableTableView]:
    raw_schema = schema_result.get("schema", {})
    raw_tables = raw_schema.get("tables", []) if isinstance(raw_schema, Mapping) else []
    if not isinstance(raw_tables, list):
        return []
    views: list[AirtableTableView] = []
    for table in raw_tables:
        if not isinstance(table, Mapping):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        raw_fields = table.get("fields", [])
        fields = []
        if isinstance(raw_fields, list):
            fields = [
                str(field.get("name") or "").strip()
                for field in raw_fields
                if isinstance(field, Mapping) and str(field.get("name") or "").strip()
            ]
        views.append(AirtableTableView(name=name, fields=tuple(fields)))
    if not views and isinstance(raw_schema, Mapping):
        allowed_tables = raw_schema.get("allowed_tables", [])
        if isinstance(allowed_tables, list):
            views = [
                AirtableTableView(name=str(table_name), fields=())
                for table_name in allowed_tables
                if str(table_name).strip()
            ]
    return views


def _mentioned_tables(normalized: str, tables: list[AirtableTableView]) -> list[AirtableTableView]:
    mentions = [
        (normalized.index(table.name.lower()), table)
        for table in tables
        if table.name.lower() in normalized
    ]
    return [table for _, table in sorted(mentions, key=lambda item: item[0])]


def _semantic_tables(normalized: str, tables: list[AirtableTableView]) -> list[AirtableTableView]:
    selected: list[AirtableTableView] = []
    for topic in _aggregate_topics(normalized):
        for table in _semantic_tables_for_topic(topic, normalized, tables):
            if table not in selected:
                selected.append(table)
    return selected


def _semantic_tables_for_topic(
    topic: str,
    normalized: str,
    tables: list[AirtableTableView],
) -> list[AirtableTableView]:
    selected: list[AirtableTableView] = []
    for table in tables:
        name = table.name.lower()
        if topic == "income" and "income" in name:
            selected.append(table)
        elif topic == "expense" and ("expense" in name or "deduction" in name or "spend" in name):
            selected.append(table)
        elif topic == "payment" and "tax" in name and ("payment" in name or "expense" in name):
            selected.append(table)
    if topic == "expense" and "tax" not in normalized:
        selected = [table for table in selected if "tax payment" not in table.name.lower()]
    if topic in {"income", "expense"}:
        if "business" in normalized and "personal" not in normalized:
            selected = [table for table in selected if "business" in table.name.lower()]
        elif "personal" in normalized and "business" not in normalized:
            selected = [table for table in selected if "personal" in table.name.lower()]
    return selected


def _looks_like_top_finance_record_request(normalized: str) -> bool:
    has_rank_word = bool(
        re.search(r"\b(top|highest|largest|biggest|max(?:imum)?|most expensive)\b", normalized)
    )
    has_finance_topic = any(
        marker in normalized for marker in ("income", "expense", "expenses", "deduction", "spend")
    )
    return has_rank_word and has_finance_topic


def _is_aggregate_request(normalized: str) -> bool:
    return (
        re.search(r"\b(total|sum|summed|summing|aggregate|add up|average|avg)\b", normalized)
        is not None
        or "how much" in normalized
        or "all income" in normalized
        or "all expenses" in normalized
        or _looks_like_finance_period_summary_request(normalized)
    )


def _is_count_request(normalized: str) -> bool:
    return re.search(r"\b(count|how many|number of)\b", normalized) is not None


def _aggregate_topic(normalized: str) -> str:
    topics = _aggregate_topics(normalized)
    return topics[0] if topics else ""


def _aggregate_topics(normalized: str) -> list[str]:
    topics: list[str] = []
    if "income" in normalized:
        topics.append("income")
    if "expense" in normalized or "deduction" in normalized or "spend" in normalized:
        topics.append("expense")
    if "payment" in normalized or "estimated tax" in normalized:
        topics.append("payment")
    if not topics and _looks_like_finance_period_summary_request(normalized):
        topics.extend(("income", "expense"))
    return list(dict.fromkeys(topics))


def _looks_like_finance_period_summary_request(normalized: str) -> bool:
    has_summary_task = bool(re.search(r"\b(?:summary|summarize|overview|snapshot)\b", normalized))
    has_period = bool(
        re.search(
            r"\b(?:current|this)\s+quarter\b|\bq[1-4]\b|\bquarter\s+[1-4]\b",
            normalized,
        )
    )
    has_finance_anchor = bool(
        re.search(r"\b(?:finance|financial|airtable|tracker|income|expenses?)\b", normalized)
    )
    return has_summary_task and has_period and has_finance_anchor


def _looks_like_rolling_tax_summary_request(normalized: str) -> bool:
    has_tax_context = "tax" in normalized or "payment" in normalized
    has_summary_context = any(
        marker in normalized
        for marker in (
            "rolling",
            "summary note",
            "tax summary",
            "tax estimate",
            "tax estimates",
            "projection",
            "planning note",
        )
    )
    return has_tax_context and has_summary_context


def _looks_like_tax_payment_or_estimate_request(normalized: str) -> bool:
    if _looks_like_wrong_response_diagnostic_request(normalized):
        return False
    if not re.search(r"\b(?:q[1-4]|quarter\s+[1-4]|20\d{2})\b", normalized):
        return False
    has_tax_subject = bool(
        re.search(
            r"\b(?:tax|taxes|irs|pennsylvania|philadelphia|philly|birt|npt|sit)\b",
            normalized,
        )
    )
    has_task = bool(
        re.search(
            r"\b(?:pay|paid|payment|payments|due|owe|owed|estimate|estimates|"
            r"estimated|calculate|calc|summarize|summary)\b",
            normalized,
        )
    )
    return has_tax_subject and has_task


def _has_clear_finance_tracker_data_anchor(normalized: str) -> bool:
    text = " ".join(str(normalized or "").lower().split())
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "finance_tax_tracker",
            "finance tax tracker",
            "airtable tracker",
            "tax tracker",
            "from the tracker",
            "from tracker",
            "from airtable",
            "airtable table",
            "airtable tables",
            "airtable record",
            "airtable records",
            "airtable schema",
            "business income",
            "business expenses",
            "personal income",
            "personal expenses",
            "tax payments table",
            "tax expenses table",
        )
    ):
        return True
    has_local_amount_task = bool(
        re.search(
            r"\b(?:paid|total|sum|summarize|summary|calculate|calc|count|"
            r"how\s+much|amount|owed|owe|remaining|record|records|rows?)\b",
            text,
        )
    )
    has_finance_table_subject = bool(
        re.search(
            r"\b(?:income|expense|expenses|deduction|spend|tax\s+payments?|tax\s+expenses?)\b",
            text,
        )
    )
    return has_local_amount_task and has_finance_table_subject


def _looks_like_wrong_response_diagnostic_request(normalized: str) -> bool:
    text = " ".join(str(normalized or "").lower().split())
    if not text:
        return False
    has_response_marker = bool(
        re.search(
            r"\b(?:same\s+response|wrong\s+(?:response|answer|lane)|"
            r"unrelated\s+(?:response|answer|output)|request\s+and\s+response|"
            r"keeps?\s+posting.*(?:answer|response)|"
            r"why\s+(?:is|did)\s+(?:this|that|it).*(?:post|posting|respond|answer))\b",
            text,
        )
    )
    has_diagnostic_marker = bool(
        re.search(
            r"\b(?:why|debug|diagnose|figure\s+out|fix|implemented|changes|"
            r"rerun|run\s+(?:it\s+)?again|keeps?\s+posting|keeps?\s+happening)\b",
            text,
        )
    )
    return has_response_marker and has_diagnostic_marker


def _money_amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | Decimal):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _mentioned_fields(normalized: str, fields: tuple[str, ...]) -> list[str]:
    mentions = [
        (normalized.index(field.lower()), field) for field in fields if field.lower() in normalized
    ]
    return [field for _, field in sorted(mentions, key=lambda item: item[0])]


def _mentioned_metric_fields(normalized: str, fields: tuple[str, ...]) -> list[str]:
    ignored_terms = (
        "quarter",
        "period",
        "date",
        "year",
        "notes",
        "method",
        "type",
        "source",
    )
    return [
        field
        for field in _mentioned_fields(normalized, fields)
        if not any(term in field.lower() for term in ignored_terms)
    ]


def _requested_quarters(normalized: str) -> tuple[int, ...]:
    quarters = {
        int(match.group(1)) for match in re.finditer(r"\bq([1-4])\b", normalized, flags=re.I)
    }
    quarters.update(
        int(match.group(1))
        for match in re.finditer(r"\bquarter\s+([1-4])\b", normalized, flags=re.I)
    )
    if re.search(r"\b(?:current|this)\s+quarter\b", normalized, flags=re.I):
        quarters.add(_current_estimated_tax_period())
    return tuple(sorted(quarters))


def _requested_years(normalized: str) -> tuple[int, ...]:
    return tuple(sorted({int(match.group(0)) for match in re.finditer(r"\b20\d{2}\b", normalized)}))


def _finance_tracker_years(normalized: str) -> tuple[int, ...]:
    years = _requested_years(normalized)
    if years:
        return years
    if re.search(r"\b(?:current|this)\s+quarter\b", normalized, flags=re.I):
        return (datetime.now(ZoneInfo("America/New_York")).year,)
    return (FINANCE_TRACKER_DEFAULT_YEAR,)


def _current_estimated_tax_period() -> int:
    now = datetime.now(ZoneInfo("America/New_York"))
    return _estimated_tax_quarter_for_month(now.month) or 1


def _effective_requested_quarters(normalized: str) -> tuple[int, ...]:
    quarters = _requested_quarters(normalized)
    if "ytd" not in normalized and "year to date" not in normalized:
        return quarters
    end_quarter = max(quarters) if quarters else _current_estimated_tax_period()
    return tuple(range(1, end_quarter + 1))


def _quarter_from_value(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    match = re.search(r"\bq?([1-4])\b", str(value), flags=re.I)
    return int(match.group(1)) if match else None


def _is_period_field_name(field_name: str) -> bool:
    normalized = str(field_name or "").lower()
    return (
        "quarter" in normalized
        or "estimated tax period" in normalized
        or normalized in {"period", "tax period", "periods"}
    )


def _year_quarter_from_date_value(value: Any) -> tuple[int | None, int | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    iso_match = re.search(r"\b(?P<year>20\d{2})-(?P<month>\d{1,2})-\d{1,2}\b", text)
    if iso_match:
        month = int(iso_match.group("month"))
        return int(iso_match.group("year")), _estimated_tax_quarter_for_month(month)
    us_match = re.search(r"\b(?P<month>\d{1,2})/\d{1,2}/(?P<year>20\d{2})\b", text)
    if us_match:
        month = int(us_match.group("month"))
        return int(us_match.group("year")), _estimated_tax_quarter_for_month(month)
    year_match = re.search(r"\b(20\d{2})\b", text)
    return (int(year_match.group(1)), None) if year_match else (None, None)


def _estimated_tax_quarter_for_month(month: int) -> int | None:
    if month < 1 or month > 12:
        return None
    if month <= 3:
        return 1
    if month <= 5:
        return 2
    if month <= 8:
        return 3
    return 4


def _record_matches_period(
    fields: Mapping[str, Any],
    *,
    quarters: tuple[int, ...],
    years: tuple[int, ...],
) -> bool:
    if not quarters and not years:
        return True
    explicit_quarter_values: list[int] = []
    date_quarter_values: list[int] = []
    year_values: list[int] = []
    for field_name, value in fields.items():
        normalized_name = str(field_name).lower()
        if _is_period_field_name(normalized_name):
            quarter = _quarter_from_value(value)
            if quarter is not None:
                explicit_quarter_values.append(quarter)
        if "date" in normalized_name or "year" in normalized_name:
            year, quarter = _year_quarter_from_date_value(value)
            if year is not None:
                year_values.append(year)
            if quarter is not None:
                date_quarter_values.append(quarter)
    if explicit_quarter_values:
        return not quarters or any(quarter in quarters for quarter in explicit_quarter_values)
    quarter_values = date_quarter_values
    quarter_ok = (
        not quarters or not quarter_values or any(quarter in quarters for quarter in quarter_values)
    )
    year_ok = not years or not year_values or any(year in years for year in year_values)
    return quarter_ok and year_ok


def _record_quarter(fields: Mapping[str, Any]) -> int | None:
    explicit_quarters: list[int] = []
    date_quarters: list[int] = []
    for field_name, value in fields.items():
        normalized_name = str(field_name).lower()
        if _is_period_field_name(normalized_name):
            quarter = _quarter_from_value(value)
            if quarter is not None:
                explicit_quarters.append(quarter)
        if "date" in normalized_name:
            _, quarter = _year_quarter_from_date_value(value)
            if quarter is not None:
                date_quarters.append(quarter)
    values = explicit_quarters or date_quarters
    return values[0] if values else None


def _wants_quarter_breakdown(normalized: str, quarters: tuple[int, ...]) -> bool:
    if len(quarters) < 2:
        return False
    return bool(re.search(r"\b(each|per|by)\s+quarter\b|\bquarterly\b", normalized))


def _record_amount(
    fields: Mapping[str, Any],
    *,
    exact_fields: list[str],
    topic: str,
) -> tuple[Decimal, list[str]]:
    if exact_fields:
        total = Decimal("0")
        used = []
        for field_name in exact_fields:
            parsed = _money_amount(fields.get(field_name))
            if parsed is None:
                continue
            total += parsed
            used.append(field_name)
        return total, used

    if topic == "expense":
        total_expenses = _money_amount(fields.get("Total Expenses"))
        if total_expenses is not None:
            return total_expenses, ["Total Expenses"]

        amount = _money_amount(fields.get("Amount"))
        if amount is not None:
            additional_taxes, tax_field = _additional_taxes_amount(fields)
            used = ["Amount"]
            if additional_taxes:
                used.append(tax_field or "Additional Taxes")
            return amount + additional_taxes, used

    amount = _money_amount(fields.get("Amount"))
    if amount is not None:
        return amount, ["Amount"]

    used_fields: list[str] = []
    total = Decimal("0")
    for field_name, value in fields.items():
        normalized = str(field_name).lower()
        if topic and topic not in normalized:
            continue
        if topic in {"income", "expense"} and "tax" in normalized:
            continue
        parsed = _money_amount(value)
        if parsed is None:
            continue
        used_fields.append(str(field_name))
        total += parsed
    return total, used_fields


def _normalize_finance_record(
    *,
    table_name: str,
    record: Mapping[str, Any],
    topic: str,
    exact_fields: list[str],
) -> NormalizedFinanceRecord | None:
    fields = record.get("fields", {})
    if not isinstance(fields, Mapping):
        return None
    amount, used_fields = _record_amount(
        fields,
        exact_fields=exact_fields,
        topic=topic,
    )
    return NormalizedFinanceRecord(
        table_name=table_name,
        record_id=str(record.get("id") or ""),
        fields=fields,
        period=_record_quarter(fields),
        amount=amount,
        amount_fields=tuple(used_fields),
    )


def _additional_taxes_amount(fields: Mapping[str, Any]) -> tuple[Decimal, str]:
    for field_name in ("Additional Taxes", "Additional taxes", "Tax Amount"):
        amount = _money_amount(fields.get(field_name))
        if amount is not None:
            return amount, field_name
    return Decimal("0"), ""


def _schema_field_by_name(
    schema_result: Mapping[str, Any],
    *,
    table_name: str,
    field_name: str,
) -> Mapping[str, Any] | None:
    raw_tables = schema_result.get("schema", {}).get("tables", [])
    if not isinstance(raw_tables, list):
        return None
    for table in raw_tables:
        if not isinstance(table, Mapping) or str(table.get("name") or "") != table_name:
            continue
        raw_fields = table.get("fields", [])
        if not isinstance(raw_fields, list):
            return None
        for field in raw_fields:
            if isinstance(field, Mapping) and str(field.get("name") or "") == field_name:
                return field
    return None


def _expense_tables_from_schema(tables: list[AirtableTableView]) -> list[AirtableTableView]:
    return [
        table
        for table in tables
        if table.name in {"Business Expenses", "Personal Expenses"}
        or ("expense" in table.name.lower() and "tax" not in table.name.lower())
    ]


def _plan_expense_total_sync_request(
    text: str,
    *,
    schema: Mapping[str, Any],
    live: bool,
    live_write: bool = False,
) -> ChiefOfStaffResult:
    tables = _expense_tables_from_schema(_schema_table_views(schema))
    if not tables:
        return _finance_tracker_result(
            text=text,
            summary=(
                "I could not find expense tables in the finance tracker schema. "
                "Please confirm the table names before syncing."
            ),
            command="Clarify expense total sync tables",
            actions=["Confirm the expense table names and retry."],
            audit_notes=["Expense total sync found no schema-matching expense tables."],
        )

    total_missing = 0
    total_mismatch = 0
    table_summaries: list[str] = []
    write_statuses: list[str] = []
    live_writes = live and live_write
    for table in tables:
        amount_field = _schema_field_by_name(schema, table_name=table.name, field_name="Amount")
        total_field = _schema_field_by_name(
            schema,
            table_name=table.name,
            field_name="Total Expenses",
        )
        if not amount_field or not total_field:
            table_summaries.append(f"{table.name}: missing Amount or Total Expenses field.")
            continue
        if bool(total_field.get("is_computed")):
            table_summaries.append(
                f"{table.name}: Total Expenses is computed; fix the Airtable formula instead."
            )
            continue
        read = airtable_read_records_impl(
            table.name,
            base_alias="finance_tax_tracker",
            fetch_all=True,
            live=live,
        )
        records = read.get("records", []) if isinstance(read, Mapping) else []
        missing_updates = []
        mismatches = []
        for record in records:
            fields = record.get("fields", {})
            if not isinstance(fields, Mapping):
                continue
            amount = _money_amount(fields.get("Amount"))
            additional_taxes, _ = _additional_taxes_amount(fields)
            total_expenses = _money_amount(fields.get("Total Expenses"))
            if amount is None:
                continue
            expected_total = amount + additional_taxes
            if total_expenses is None:
                record_id = str(record.get("id") or "")
                if record_id:
                    missing_updates.append((record_id, expected_total))
                continue
            if total_expenses != expected_total:
                mismatches.append(str(record.get("id") or "unknown"))
        for record_id, amount in missing_updates:
            preview = airtable_write_record_impl(
                json.dumps({"Total Expenses": float(amount)}, sort_keys=True),
                table=table.name,
                base_alias="finance_tax_tracker",
                record_id=record_id,
                approval_reference=(
                    "chief-of-staff-expense-total-sync-approved-live"
                    if live_writes
                    else "chief-of-staff-expense-total-sync-preview"
                ),
                operation="update",
                live=live_writes,
            )
            write_statuses.append(str(preview.get("status", "unknown")))
        total_missing += len(missing_updates)
        total_mismatch += len(mismatches)
        mismatch_count = len(mismatches)
        table_summaries.append(
            f"{table.name}: {_plural_count(len(missing_updates), 'blank Total Expenses value')} "
            f"can be backfilled from Amount; "
            f"{_plural_count(mismatch_count, 'nonblank mismatch', 'nonblank mismatches')} "
            f"{'needs' if mismatch_count == 1 else 'need'} review."
        )
    write_sentence = (
        "Live writes were requested for blank values only."
        if live_writes
        else "No live write was performed."
    )
    summary = (
        "I reviewed the expense tables against the live schema. `Total Expenses` is a "
        "manual currency field, so blank values will not fill themselves unless Airtable "
        "has a formula or automation. Based on the documented column semantics, the "
        "expected value is Amount plus Additional Taxes. "
        f"I found {_plural_count(total_missing, 'blank Total Expenses value')} that can be "
        f"backfilled and {_plural_count(total_mismatch, 'nonblank mismatch')} that should "
        f"be reviewed before overwriting. "
        f"{' '.join(table_summaries)} {write_sentence}"
    )
    return _finance_tracker_result(
        text=text,
        summary=summary,
        command=(
            "Live update blank Total Expenses from Amount"
            if live_writes
            else "Prepare dry-run Total Expenses sync from Amount"
        ),
        actions=[
            (
                "Review the completed updates and the one mismatch before relying on totals."
                if live_writes
                else "Review the dry-run sync plan before allowing live Airtable updates."
            ),
            (
                "For recurring consistency, consider changing Total Expenses to an Airtable "
                "formula or automation rather than a separate manual field."
            ),
            (
                "Live sync requires AIRTABLE_ALLOW_WRITES=true, AIRTABLE_WRITE_DRY_RUN=false, "
                "and an approval reference."
            ),
        ],
        audit_notes=[
            (
                "Expense total review used live schema, capped record reads, and "
                "documented field semantics."
            ),
            (
                "Only blank Total Expenses fields were included in live update requests."
                if live_writes
                else "Only blank Total Expenses fields were included in dry-run update previews."
            ),
            f"Write statuses: {', '.join(write_statuses) or 'none'}.",
        ],
    )


def _schema_fields_for_named_table(
    schema_result: Mapping[str, Any],
    table_name: str,
) -> list[Mapping[str, Any]]:
    tables = schema_result.get("schema", {}).get("tables", [])
    if not isinstance(tables, list):
        return []
    for table in tables:
        if not isinstance(table, Mapping) or table.get("name") != table_name:
            continue
        fields = table.get("fields", [])
        return [field for field in fields if isinstance(field, Mapping)] if isinstance(fields, list) else []
    return []


def _finance_receipt_schema_mapping_summary(
    schema_mapping: Mapping[str, Any],
    schema_mapping_status: str,
) -> str:
    if not schema_mapping:
        return ""
    fields = schema_mapping.get("fields")
    attachment = schema_mapping.get("attachment_field")
    select_candidates = schema_mapping.get("select_candidates")
    field_names = ", ".join(sorted(fields)) if isinstance(fields, Mapping) else ""
    attachment_name = (
        str(attachment.get("name") or "")
        if isinstance(attachment, Mapping)
        else ""
    )
    select_names = (
        ", ".join(sorted(select_candidates))
        if isinstance(select_candidates, Mapping) and select_candidates
        else ""
    )
    parts = []
    if field_names:
        parts.append(f"schema-backed fields: {field_names}")
    if attachment_name:
        parts.append(f"attachment field: {attachment_name}")
    if select_names:
        parts.append(f"review select candidates for: {select_names}")
    if not parts:
        return ""
    status = f" ({schema_mapping_status})" if schema_mapping_status else ""
    return " Schema mapping resolved" + status + ": " + "; ".join(parts) + "."


def _plural_count(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f"1 {singular}"
    return f"{count} {plural or singular + 's'}"


def _format_money(amount: Decimal) -> str:
    return f"${amount.quantize(Decimal('0.01')):,.2f}"


def _finance_tracker_tax_table_name(
    text: str,
    *,
    schema_tables: list[AirtableTableView] | None = None,
) -> str:
    normalized = _normalized_text(text)
    tables = schema_tables or []
    for table in tables:
        if table.name.lower() in normalized and "tax" in table.name.lower():
            return table.name
    for candidate in FINANCE_TRACKER_TAX_TABLE_CANDIDATES:
        if candidate.lower() in normalized:
            return candidate
    for table in tables:
        table_name = table.name.lower()
        if "tax" in table_name and any(term in table_name for term in ("payment", "expense")):
            return table.name
    return FINANCE_TRACKER_TAX_TABLE_CANDIDATES[0]


def _tax_type_bucket(value: Any) -> str:
    if isinstance(value, list | tuple | set):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value or "")
    normalized = text.lower()
    if "federal" in normalized or "irs" in normalized:
        return "Federal"
    if "state" in normalized or "pennsylvania" in normalized or normalized.strip() == "pa":
        return "Pennsylvania"
    if (
        "city" in normalized
        or "philadelphia" in normalized
        or "philly" in normalized
        or "birt" in normalized
        or "npt" in normalized
        or "sit" in normalized
    ):
        return "Philadelphia"
    return "Unclassified"


def _field_text(fields: Mapping[str, Any], *names: str) -> str:
    values = []
    for name in names:
        value = fields.get(name)
        if value is None:
            continue
        if isinstance(value, list | tuple | set):
            values.extend(str(item) for item in value if item is not None)
        else:
            values.append(str(value))
    return " ".join(values)


def _is_rolling_tax_summary_record(fields: Mapping[str, Any]) -> bool:
    text = _field_text(fields, "Payment Name", "Notes").lower()
    if "rolling" in text and ("tax" in text or "estimate" in text or "summary" in text):
        return True
    return "tax summary" in text or "tax estimate" in text or "planning estimate" in text


def _rolling_summary_name(fields: Mapping[str, Any]) -> str:
    return str(fields.get("Payment Name") or fields.get("Name") or "Rolling tax summary")


def _note_contains_tax_planning_sections(fields: Mapping[str, Any]) -> list[str]:
    note = str(fields.get("Notes") or "").lower()
    sections = []
    checks = (
        ("YTD income", ("ytd income", "total 2026 ytd income")),
        ("self-employment income", ("self-employment", "1099")),
        ("investment income", ("investment income", "dividends", "capital gains")),
        ("business expenses", ("business expenses", "deductible")),
        ("federal estimate/payment", ("federal", "irs")),
        ("Pennsylvania estimate/payment", ("pennsylvania", "pa estimated", "state tax")),
        ("Philadelphia NPT/SIT estimate", ("philadelphia", "philly", "npt", "sit")),
        ("assumptions/caveats", ("notes", "estimate uses", "planning estimate", "accountant")),
    )
    for label, markers in checks:
        if any(marker in note for marker in markers):
            sections.append(label)
    return sections


def _plan_tax_payment_summary_request(
    *,
    text: str,
    normalized: str,
    schema: Mapping[str, Any],
    live: bool,
) -> ChiefOfStaffResult:
    tax_table = _finance_tracker_tax_table_name(
        text,
        schema_tables=_schema_table_views(schema),
    )
    quarters = _effective_requested_quarters(normalized)
    years = _finance_tracker_years(normalized)
    read = airtable_read_records_impl(
        tax_table,
        base_alias="finance_tax_tracker",
        fetch_all=True,
        live=live,
    )
    records = read.get("records", []) if isinstance(read, Mapping) else []
    paid_totals = {
        "Federal": Decimal("0"),
        "Pennsylvania": Decimal("0"),
        "Philadelphia": Decimal("0"),
        "Unclassified": Decimal("0"),
    }
    paid_counts = {key: 0 for key in paid_totals}
    rolling_names: list[str] = []
    rolling_sections: set[str] = set()
    skipped_summary_rows = 0
    skipped_placeholders = 0

    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, Mapping) or not _record_matches_period(
            fields,
            quarters=quarters,
            years=years,
        ):
            continue
        if _is_rolling_tax_summary_record(fields):
            skipped_summary_rows += 1
            rolling_names.append(_rolling_summary_name(fields))
            rolling_sections.update(_note_contains_tax_planning_sections(fields))
            continue
        amount = _money_amount(fields.get("Amount"))
        if amount is None or amount == Decimal("0"):
            skipped_placeholders += 1
            continue
        bucket = _tax_type_bucket(fields.get("Tax Type"))
        paid_totals[bucket] += amount
        paid_counts[bucket] += 1

    period = _aggregate_period_label(quarters=quarters, years=years) or "the requested period"
    paid_parts = [
        f"Federal: {_format_money(paid_totals['Federal'])}",
        f"Pennsylvania: {_format_money(paid_totals['Pennsylvania'])}",
        f"Philadelphia: {_format_money(paid_totals['Philadelphia'])}",
    ]
    unclassified = paid_totals["Unclassified"]
    if unclassified:
        paid_parts.append(f"Unclassified: {_format_money(unclassified)}")
    total_paid = sum(paid_totals.values(), Decimal("0"))
    wants_estimate_context = any(
        marker in normalized
        for marker in (
            "estimate",
            "estimated",
            "due",
            "remaining",
            "owe",
            "planning",
            "calculate taxes",
        )
    )
    lines = [
        f"{period} tax payments",
        "",
        f"Total paid: {_format_money(total_paid)}",
        "",
        "By tax type",
        *[f"* {part}" for part in paid_parts],
    ]
    if skipped_summary_rows or skipped_placeholders:
        excluded_parts = []
        if skipped_summary_rows:
            excluded_parts.append(
                f"{skipped_summary_rows} rolling summary/helper "
                f"record{'s' if skipped_summary_rows != 1 else ''}"
            )
        if skipped_placeholders:
            excluded_parts.append(
                f"{skipped_placeholders} blank/zero placeholder "
                f"record{'s' if skipped_placeholders != 1 else ''}"
            )
        lines.extend(["", f"Excluded from paid total: {'; '.join(excluded_parts)}."])
    if rolling_names:
        lines.extend(["", f"Reference note: {', '.join(rolling_names)}."])
    if wants_estimate_context and rolling_sections:
        lines.extend(
            [
                "",
                "Rolling note includes: "
                + ", ".join(sorted(rolling_sections))
                + ". Reconcile it against current income and expense tables before "
                "using it for due/remaining estimates.",
            ]
        )
    summary = "\n".join(lines)
    return _finance_tracker_result(
        text=text,
        summary=summary,
        command="Summarize period tax payments and rolling tax planning notes",
        actions=[
            "Keep paid-tax totals separate from estimated due or remaining tax.",
            (
                "For Q2 estimates, reconcile the rolling summary note against current "
                "income and expense records before recommending a payment amount."
            ),
            (
                "Set `needs_human_tax_review=true` for Philadelphia NPT/SIT/BIRT, "
                "uncertain deductions, or unclear capital-gain treatment."
            ),
        ],
        audit_notes=[
            "Schema-first Tax Payments read used normalized task records.",
            "Paid totals exclude rolling summary/helper rows and zero-dollar placeholders.",
            "Deterministic arithmetic computed paid amounts; estimate interpretation is advisory.",
        ],
    )


def _summarize_table_read(
    *,
    text: str,
    table: str,
    read: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> ChiefOfStaffResult:
    fields = _field_names_from_records(records)
    review_note = (
        "Business-vs-personal categorization and deduction treatment require human review."
        if "expense" in table.lower()
        else "Income classification should be reviewed before tax estimates or filings."
        if "income" in table.lower()
        else "Estimated-payment status and Tax Type require CPA/human tax review when uncertain."
    )
    summary = (
        f"`{table}` live read status: `{read.get('status', 'unknown')}`; "
        f"records returned: {len(records)}. "
        f"Visible field names: {', '.join(fields) if fields else 'none returned'}. "
        f"{review_note} No dollar amounts are included here."
    )
    return _finance_tracker_result(
        text=text,
        summary=summary,
        command=f"Read capped records from {table}",
        actions=[
            f"Use `{table}` only for its scoped Airtable purpose.",
            "Flag uncertain tax treatment with `needs_human_tax_review=true` when relevant.",
            "Do not write without exact field mapping and approval reference.",
        ],
        audit_notes=["Airtable read was capped at 1 record and values were not summarized."],
    )


def _aggregate_period_label(
    *,
    quarters: tuple[int, ...],
    years: tuple[int, ...],
) -> str:
    parts = []
    if quarters:
        parts.append(" and ".join(f"Q{quarter}" for quarter in quarters))
    if years:
        parts.append(" and ".join(str(year) for year in years))
    return " ".join(parts).strip()


def _aggregate_topic_tables(
    *,
    topic: str,
    normalized: str,
    tables: list[AirtableTableView],
) -> list[AirtableTableView]:
    mentioned = [
        table
        for table in _mentioned_tables(normalized, tables)
        if table in _semantic_tables_for_topic(topic, normalized, tables)
    ]
    return mentioned or _semantic_tables_for_topic(topic, normalized, tables)


def _compute_airtable_topic_aggregate(
    *,
    topic: str,
    normalized: str,
    tables: list[AirtableTableView],
    quarters: tuple[int, ...],
    years: tuple[int, ...],
    live: bool,
) -> tuple[
    Decimal,
    list[str],
    list[AirtableTableView],
    dict[str, Decimal],
    int,
]:
    selected_tables = _aggregate_topic_tables(topic=topic, normalized=normalized, tables=tables)
    total = Decimal("0")
    table_parts: list[str] = []
    category_totals: dict[str, Decimal] = {}
    uncategorized_count = 0

    for table in selected_tables:
        exact_fields = _mentioned_metric_fields(normalized, table.fields)
        read = airtable_read_records_impl(
            table.name,
            base_alias="finance_tax_tracker",
            fetch_all=True,
            live=live,
        )
        records = read.get("records", []) if isinstance(read, Mapping) else []
        matched_count = 0
        contributing_count = 0
        subtotal = Decimal("0")
        used_fields: set[str] = set()
        for record in records:
            fields = record.get("fields", {})
            if not isinstance(fields, Mapping) or not _record_matches_period(
                fields,
                quarters=quarters,
                years=years,
            ):
                continue
            normalized_record = _normalize_finance_record(
                table_name=table.name,
                record=record,
                exact_fields=exact_fields,
                topic=topic,
            )
            if normalized_record is None:
                continue
            subtotal += normalized_record.amount
            if normalized_record.amount_fields:
                used_fields.update(normalized_record.amount_fields)
                matched_count += 1
                if normalized_record.amount != Decimal("0"):
                    contributing_count += 1
            if topic == "expense" and normalized_record.amount_fields:
                category = next(
                    (
                        str(fields.get(field_name) or "").strip()
                        for field_name in ("Categories", "Category")
                        if str(fields.get(field_name) or "").strip()
                    ),
                    "",
                )
                if category:
                    category_totals[category] = (
                        category_totals.get(category, Decimal("0"))
                        + normalized_record.amount
                    )
                else:
                    uncategorized_count += 1
        total += subtotal
        truncation_note = (
            f"; read truncated at {read.get('record_limit')} records"
            if isinstance(read, Mapping) and read.get("truncated")
            else ""
        )
        contribution_text = (
            "1 contributed a non-zero amount"
            if contributing_count == 1
            else f"{contributing_count} contributed non-zero amounts"
        )
        contribution_note = (
            f"; {contribution_text}, totaling {_format_money(subtotal)}"
            if contributing_count != matched_count
            else f", totaling {_format_money(subtotal)}"
        )
        table_parts.append(
            f"{_finance_tracker_table_label(table.name, topic=topic)}: "
            f"{matched_count} matching records{contribution_note}"
            f"{truncation_note}"
        )
    return total, table_parts, selected_tables, category_totals, uncategorized_count


def _finance_tracker_topic_label(topic: str) -> str:
    return "expenses" if topic == "expense" else topic


def _finance_tracker_table_label(table_name: str, *, topic: str) -> str:
    normalized_name = _normalized_text(table_name)
    if topic == "income" and "personal income" in normalized_name:
        return "Investment / personal income"
    return table_name.lower()


def _finance_record_display_value(
    fields: Mapping[str, Any],
    candidate_names: tuple[str, ...],
) -> str:
    normalized_candidates = {_normalized_text(name) for name in candidate_names}
    for field_name, value in fields.items():
        if _normalized_text(str(field_name)) not in normalized_candidates:
            continue
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            return ", ".join(str(item) for item in value if str(item).strip())
        return str(value)
    return ""


def _plan_top_airtable_record_from_schema(
    *,
    text: str,
    normalized: str,
    tables: list[AirtableTableView],
    live: bool,
) -> ChiefOfStaffResult | None:
    if not _looks_like_top_finance_record_request(normalized):
        return None

    topic = _aggregate_topic(normalized) or "expense"
    selected_tables = _mentioned_tables(normalized, tables) or _semantic_tables(normalized, tables)
    if not selected_tables:
        return _finance_tracker_result(
            text=text,
            summary=(
                "I could not map the top-record request to an allowed finance tracker "
                "table from the live schema. Please name the table."
            ),
            command="Clarify Airtable top-record table",
            actions=[
                "Name the Airtable table, such as Business Expenses.",
                "Name the amount field if the metric is not obvious from the schema.",
            ],
            audit_notes=["Schema-first top-record planning did not find table matches."],
        )

    quarters = _effective_requested_quarters(normalized)
    years = _finance_tracker_years(normalized)
    best_record: NormalizedFinanceRecord | None = None
    best_table = ""
    table_counts: dict[str, int] = {}
    matched_counts: dict[str, int] = {}

    for table in selected_tables:
        exact_fields = _mentioned_metric_fields(normalized, table.fields)
        read = airtable_read_records_impl(
            table.name,
            base_alias="finance_tax_tracker",
            fetch_all=True,
            live=live,
        )
        records = read.get("records", []) if isinstance(read, Mapping) else []
        table_counts[table.name] = len(records)
        matched_counts[table.name] = 0
        for record in records:
            fields = record.get("fields", {})
            if not isinstance(fields, Mapping) or not _record_matches_period(
                fields,
                quarters=quarters,
                years=years,
            ):
                continue
            normalized_record = _normalize_finance_record(
                table_name=table.name,
                record=record,
                exact_fields=exact_fields,
                topic=topic,
            )
            if normalized_record is None or not normalized_record.amount_fields:
                continue
            matched_counts[table.name] += 1
            if best_record is None or normalized_record.amount > best_record.amount:
                best_record = normalized_record
                best_table = table.name

    if best_record is None:
        table_names = ", ".join(f"`{table.name}`" for table in selected_tables)
        return _finance_tracker_result(
            text=text,
            summary=(
                "I did not find any matching numeric finance tracker records for the "
                f"top {topic or 'amount'} request in {table_names}."
            ),
            command="Find top Airtable finance record",
            actions=[
                "Check whether the target table has Amount or Total Expenses populated.",
                "Specify a quarter or year if the request should be period-limited.",
            ],
            audit_notes=["Schema-first top-record planning found no numeric matches."],
        )

    fields = best_record.fields
    item = _finance_record_display_value(fields, ("Item", "Payment Name", "Name"))
    date = _finance_record_display_value(
        fields,
        ("Date of Expense", "Pay Date", "Payment Date", "Date"),
    )
    category = _finance_record_display_value(fields, ("Categories", "Category"))
    vendor = _finance_record_display_value(
        fields,
        ("Expense Client/Vendor", "Vendor", "Source"),
    )
    description = _finance_record_display_value(fields, ("Description", "Notes"))
    period = f"Q{best_record.period}" if best_record.period else ""
    detail_lines = [
        f"* Table: {best_table}",
        f"* Amount used: {_format_money(best_record.amount)}",
        f"* Field used: {', '.join(best_record.amount_fields)}",
    ]
    if item:
        detail_lines.insert(0, f"* Item: {item}")
    if date:
        detail_lines.append(f"* Date: {date}")
    if period:
        detail_lines.append(f"* Estimated tax period: {period}")
    if category:
        detail_lines.append(f"* Category: {category}")
    if vendor:
        detail_lines.append(f"* Vendor/source: {vendor}")
    if description and description != item:
        detail_lines.append(f"* Description: {description}")
    if best_record.record_id:
        detail_lines.append(f"* Airtable record id: {best_record.record_id}")

    table_summary = ", ".join(
        f"{table.name}: {matched_counts.get(table.name, 0)} matching numeric records"
        for table in selected_tables
    )
    period_label = _aggregate_period_label(quarters=quarters, years=years)
    title_context = f" for {period_label}" if period_label else ""
    summary = (
        f"Top {topic or 'finance'} record in finance_tax_tracker{title_context}\n\n"
        f"The largest matching record I found is {_format_money(best_record.amount)}.\n\n"
        "Detail\n\n" + "\n".join(detail_lines) + "\n\nChecks\n\n"
        f"* Records checked: {table_summary}"
    )
    return _finance_tracker_result(
        text=text,
        summary=summary,
        command="Find top Airtable finance record from schema-selected tables",
        actions=[
            "Review the selected table and amount field before relying on the result.",
            "Specify a quarter or year if the top record should be period-limited.",
            "Flag deduction treatment or business-purpose uncertainty for human tax review.",
        ],
        audit_notes=[
            "Schema-first Airtable top-record planning selected tables from live schema.",
            "No Airtable mutation was attempted.",
            "Amounts were normalized locally from typed Airtable read results.",
        ],
    )


def _plan_airtable_aggregate_from_schema(
    *,
    text: str,
    normalized: str,
    tables: list[AirtableTableView],
    live: bool,
) -> ChiefOfStaffResult | None:
    if not (_is_aggregate_request(normalized) or _is_count_request(normalized)):
        return None

    topics = _aggregate_topics(normalized)
    quarters = _effective_requested_quarters(normalized)
    years = _finance_tracker_years(normalized)
    if _is_aggregate_request(normalized) and len(topics) > 1:
        totals_by_topic: dict[str, Decimal] = {}
        details_by_topic: dict[str, list[str]] = {}
        expense_category_totals: dict[str, Decimal] = {}
        uncategorized_expense_count = 0
        selected_any: list[AirtableTableView] = []
        for topic in topics:
            (
                total,
                table_parts,
                selected_tables,
                category_totals,
                uncategorized_count,
            ) = _compute_airtable_topic_aggregate(
                topic=topic,
                normalized=normalized,
                tables=tables,
                quarters=quarters,
                years=years,
                live=live,
            )
            if not selected_tables:
                continue
            selected_any.extend(selected_tables)
            totals_by_topic[topic] = total
            details_by_topic[topic] = table_parts
            if topic == "expense":
                expense_category_totals = category_totals
                uncategorized_expense_count = uncategorized_count
        if not selected_any:
            return _finance_tracker_result(
                text=text,
                summary=(
                    "I could not map the requested income/expense totals to allowed "
                    "Airtable tables from the live schema. Please name the tables."
                ),
                command="Clarify Airtable aggregate tables",
                actions=[
                    "Name one or more Airtable tables.",
                    "Name the numeric field if the metric is not obvious from the schema.",
                ],
                audit_notes=["Schema-first multi-aggregate planning did not find table matches."],
            )
        period = _aggregate_period_label(quarters=quarters, years=years)
        heading = (
            f"{period} finance_tax_tracker Summary" if period else "finance_tax_tracker Summary"
        )
        intro = (
            f"For {period}, the finance_tax_tracker currently shows:"
            if period
            else "The finance_tax_tracker currently shows:"
        )
        sections = [heading, "", intro, "", "Totals", ""]
        for topic in topics:
            if topic not in totals_by_topic:
                continue
            label = _finance_tracker_topic_label(topic)
            sections.append(f"* Total {label}: {_format_money(totals_by_topic[topic])}")
        for topic in topics:
            table_parts = details_by_topic.get(topic, [])
            if not table_parts:
                continue
            label = _finance_tracker_topic_label(topic)
            section_title = "Expense detail" if topic == "expense" else f"{label.title()} detail"
            sections.extend(["", section_title, ""])
            sections.append(f"* Combined {label}: {_format_money(totals_by_topic[topic])}")
            sections.extend(f"* {part}" for part in table_parts)
        if expense_category_totals or "expense" in totals_by_topic:
            sections.extend(["", "Expense categories", ""])
            if expense_category_totals:
                sections.extend(
                    f"* {category}: {_format_money(amount)}"
                    for category, amount in sorted(expense_category_totals.items())
                )
            else:
                sections.append("* No categorized expense amounts matched the requested period.")
            sections.append(
                "* Uncategorized gap: "
                f"{uncategorized_expense_count} matching expense "
                f"record{'s' if uncategorized_expense_count != 1 else ''}."
            )
        summary = "\n".join(sections)
        return _finance_tracker_result(
            text=text,
            summary=summary,
            command="Aggregate multiple Airtable finance metrics from schema-selected tables",
            actions=[
                "Review table, field, and quarter assumptions before relying on the result.",
                (
                    "Live aggregate reads paginate through Airtable records up to "
                    "AIRTABLE_READ_ALL_MAX_RECORDS; export if any table reports truncation."
                ),
                (
                    "Flag tax treatment, classification, and estimated-payment questions "
                    "for human review."
                ),
            ],
            audit_notes=[
                "Schema-first Airtable multi-aggregate planning selected tables from live schema.",
                "No Airtable mutation was attempted.",
                "Arithmetic was computed locally from typed Airtable read results.",
            ],
        )

    selected_tables = _mentioned_tables(normalized, tables) or _semantic_tables(normalized, tables)
    if not selected_tables:
        return _finance_tracker_result(
            text=text,
            summary=(
                "I could not map the aggregate request to a specific allowed Airtable "
                "table from the live schema. Please name the table or metric to aggregate."
            ),
            command="Clarify Airtable aggregate table",
            actions=[
                "Name one or more Airtable tables.",
                "Name the numeric field if the metric is not obvious from the schema.",
            ],
            audit_notes=["Schema-first aggregate planning did not find a table match."],
        )

    topic = _aggregate_topic(normalized)
    table_totals: dict[str, Decimal] = {}
    table_counts: dict[str, int] = {}
    table_matching_counts: dict[str, int] = {}
    table_contributing_counts: dict[str, int] = {}
    field_usage: dict[str, set[str]] = {}
    statuses: dict[str, str] = {}
    quarters = _effective_requested_quarters(normalized)
    years = _finance_tracker_years(normalized)
    quarter_breakdown_requested = _wants_quarter_breakdown(normalized, quarters)
    quarter_totals: dict[int, Decimal] = {quarter: Decimal("0") for quarter in quarters}
    quarter_counts: dict[int, int] = {quarter: 0 for quarter in quarters}

    for table in selected_tables:
        exact_fields = _mentioned_metric_fields(normalized, table.fields)
        read = airtable_read_records_impl(
            table.name,
            base_alias="finance_tax_tracker",
            fetch_all=True,
            live=live,
        )
        records = read.get("records", []) if isinstance(read, Mapping) else []
        statuses[table.name] = str(read.get("status", "unknown"))
        table_counts[table.name] = len(records)
        table_matching_counts[table.name] = 0
        table_contributing_counts[table.name] = 0
        table_totals[table.name] = Decimal("0")
        field_usage[table.name] = set()
        if _is_count_request(normalized) and not _is_aggregate_request(normalized):
            continue
        for record in records:
            fields = record.get("fields", {})
            if not isinstance(fields, Mapping) or not _record_matches_period(
                fields,
                quarters=quarters,
                years=years,
            ):
                continue
            normalized_record = _normalize_finance_record(
                table_name=table.name,
                record=record,
                exact_fields=exact_fields,
                topic=topic,
            )
            if normalized_record is None:
                continue
            table_totals[table.name] += normalized_record.amount
            field_usage[table.name].update(normalized_record.amount_fields)
            if normalized_record.amount_fields:
                table_matching_counts[table.name] += 1
                if normalized_record.amount != Decimal("0"):
                    table_contributing_counts[table.name] += 1
            if quarter_breakdown_requested:
                quarter = normalized_record.period
                if quarter in quarter_totals:
                    quarter_totals[quarter] += normalized_record.amount
                    if normalized_record.amount_fields:
                        quarter_counts[quarter] += 1

    if _is_count_request(normalized) and not _is_aggregate_request(normalized):
        total_count = sum(table_counts.values())
        table_parts = [
            f"{table_counts[table.name]} from `{table.name}`" for table in selected_tables
        ]
        summary = (
            f"I found {total_count} record{'s' if total_count != 1 else ''} in "
            f"`finance_tax_tracker`: {', '.join(table_parts)}. This was read-only."
        )
        command = "Count Airtable records from schema-selected tables"
    else:
        total = sum(table_totals.values(), Decimal("0"))
        table_parts = []
        for table in selected_tables:
            fields_used = (
                ", ".join(sorted(field_usage[table.name])) or "none with numeric values matched"
            )
            truncation_note = (
                f"; read truncated at {read.get('record_limit')} records"
                if isinstance(read, Mapping) and read.get("truncated")
                else ""
            )
            matched_count = table_matching_counts[table.name]
            contributing_count = table_contributing_counts[table.name]
            contribution_text = (
                "1 contributed a non-zero amount"
                if contributing_count == 1
                else f"{contributing_count} contributed non-zero amounts"
            )
            contribution_note = (
                f"; {contribution_text}, totaling {_format_money(table_totals[table.name])}"
                if contributing_count != matched_count
                else f", totaling {_format_money(table_totals[table.name])}"
            )
            table_parts.append(
                f"`{table.name}` had {matched_count} matching records"
                f"{contribution_note} from {fields_used}{truncation_note}"
            )
        if quarter_breakdown_requested:
            quarter_parts = [
                f"Q{quarter}: {_format_money(quarter_totals[quarter])}"
                f" ({quarter_counts[quarter]} matching records)"
                for quarter in quarters
            ]
            summary = (
                f"{topic.title() if topic else 'Amount'} totals by quarter in "
                f"`finance_tax_tracker`:\n\n"
                + "\n".join(f"- {part}" for part in quarter_parts)
                + "\n\nDetail\n"
                + "\n".join(f"- {part}" for part in table_parts)
            )
            command = "Aggregate Airtable records by quarter from schema-selected tables"
            return _finance_tracker_result(
                text=text,
                summary=summary,
                command=command,
                actions=[
                    (
                        "Review the table, field, and quarter assumptions before relying "
                        "on the result."
                    ),
                    (
                        "Live aggregate reads paginate through Airtable records up to "
                        "AIRTABLE_READ_ALL_MAX_RECORDS; export if any table reports truncation."
                    ),
                    (
                        "Flag tax treatment, classification, and estimated-payment questions "
                        "for human review."
                    ),
                ],
                audit_notes=[
                    "Schema-first Airtable aggregate planning selected tables from live schema.",
                    "No Airtable mutation was attempted.",
                    "Arithmetic was computed locally from typed Airtable read results.",
                ],
            )
        summary = (
            f"Total {topic or 'amount'} in the `finance_tax_tracker` Airtable base is "
            f"{_format_money(total)}.\n\n"
            "Detail\n" + "\n".join(f"- {part}" for part in table_parts)
        )
        command = "Aggregate Airtable records from schema-selected tables"

    return _finance_tracker_result(
        text=text,
        summary=summary,
        command=command,
        actions=[
            "Review the table and field assumptions before relying on the result.",
            (
                "Live aggregate reads paginate through Airtable records up to "
                "AIRTABLE_READ_ALL_MAX_RECORDS; export if any table reports truncation."
            ),
            "Flag tax treatment, classification, and estimated-payment questions for human review.",
        ],
        audit_notes=[
            "Schema-first Airtable aggregate planning selected tables from live schema.",
            "No Airtable mutation was attempted.",
            "Arithmetic was computed locally from typed Airtable read results.",
        ],
    )


def _plan_finance_tracker_request(text: str, *, live: bool) -> ChiefOfStaffResult:
    active_text = _active_slack_followup_text(text)
    normalized = _normalized_text(active_text)
    command = active_text[:240]
    try:
        if _finance_expense_receipt_provider_context(active_text):
            return _plan_finance_expense_receipt_create_request(active_text, live=live)

        if _looks_like_expense_total_sync_request(normalized):
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            return _plan_expense_total_sync_request(
                active_text,
                schema=schema,
                live=live,
                live_write=False,
            )

        if "dry-run" in normalized or "dry run" in normalized or "write plan" in normalized:
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            tax_table = _finance_tracker_tax_table_name(
                active_text,
                schema_tables=_schema_table_views(schema),
            )
            fields = {
                "Payment Name": "KNI CoS dry-run test",
                "Estimated Tax Periods": 1,
                "Amount": 0,
                "Payment Date": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                "Notes": (
                    "TEST RECORD: dry-run Chief of Staff Airtable write plan only; "
                    "do not use for tax reporting."
                ),
            }
            preview = airtable_write_record_impl(
                json.dumps(fields, sort_keys=True),
                table=tax_table,
                base_alias="finance_tax_tracker",
                approval_reference="slack-chief-of-staff-dry-run-finance-tax-tracker-test",
                operation="create",
                live=False,
            )
            summary = (
                "Dry-run Airtable write plan prepared for `finance_tax_tracker` / "
                f"`{tax_table}`. Fields: Payment Name, Estimated Tax Periods, Amount, "
                "Payment Date, Notes. Approval reference: "
                "`slack-chief-of-staff-dry-run-finance-tax-tracker-test`. "
                f"Tool status: `{preview.get('status', 'dry-run')}`. No live write occurred."
            )
            return _finance_tracker_result(
                text=text,
                summary=summary,
                command="Prepare dry-run Tax Payments write plan",
                actions=[
                    "Review the exact test fields before any live create.",
                    (
                        "For a live create, set write gates and provide a "
                        "command-scoped approval reference."
                    ),
                    "Keep zero-dollar test rows clearly marked as excluded from tax reporting.",
                ],
                audit_notes=[
                    "Schema/text-aware tax table resolution was used for the dry-run plan.",
                    "Airtable write tool was called in dry-run mode only.",
                ],
            )

        if "kni_ops" in normalized or "kni ops" in normalized:
            finance = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            ops = airtable_get_base_schema_impl(base_alias="kni_ops", live=live)
            finance_tables = [
                table["name"] for table in finance.get("schema", {}).get("tables", [])
            ]
            ops_tables = [table["name"] for table in ops.get("schema", {}).get("tables", [])]
            summary = (
                "`finance_tax_tracker` tables: "
                f"{', '.join(finance_tables) or 'unavailable'}. "
                "`kni_ops` tables: "
                f"{', '.join(ops_tables) or 'unavailable'}. "
                "Guardrail: never infer a record belongs to the other base; "
                "choose the base alias first."
            )
            return _finance_tracker_result(
                text=text,
                summary=summary,
                command="Compare finance_tax_tracker and kni_ops Airtable base schemas",
                actions=[
                    'Use `base_alias="finance_tax_tracker"` for finance/tax records.',
                    'Use `base_alias="kni_ops"` for operations records.',
                    "Ask for clarification before reading or writing if the base is ambiguous.",
                ],
                audit_notes=["Live schema reads attempted for both configured Airtable bases."],
            )

        record_id_match = re.search(r"\brec[a-zA-Z0-9]{8,}\b", text)
        if record_id_match:
            record_id = record_id_match.group(0)
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            tax_table = _finance_tracker_tax_table_name(
                active_text,
                schema_tables=_schema_table_views(schema),
            )
            read = airtable_read_records_impl(
                tax_table,
                base_alias="finance_tax_tracker",
                filter_formula=f"RECORD_ID()='{record_id}'",
                max_records=1,
                live=live,
            )
            records = read.get("records", []) if isinstance(read, Mapping) else []
            fields = _field_names_from_records(records)
            summary = (
                f"{tax_table} record `{record_id}` "
                f"{'was found' if records else 'was not found'} in `finance_tax_tracker`. "
                f"Non-sensitive field names: {', '.join(fields) if fields else 'none returned'}."
            )
            return _finance_tracker_result(
                text=text,
                summary=summary,
                command=f"Verify Tax Payments record {record_id}",
                actions=[
                    "Treat the test row as operational verification only.",
                    "Do not use zero-dollar test rows for tax reporting.",
                ],
                audit_notes=[
                    "Schema/text-aware tax table resolution was used for record lookup.",
                    "Airtable read was filtered by RECORD_ID().",
                ],
            )

        table = _finance_tracker_table_from_text(active_text)
        schema: Mapping[str, Any] | None = None
        if (
            _is_aggregate_request(normalized)
            or _is_count_request(normalized)
            or _looks_like_top_finance_record_request(normalized)
            or _looks_like_tax_payment_or_estimate_request(normalized)
            or not table
        ):
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
            schema_tables = _schema_table_views(schema)
            if _looks_like_tax_payment_or_estimate_request(normalized):
                return _plan_tax_payment_summary_request(
                    text=active_text,
                    normalized=normalized,
                    schema=schema,
                    live=live,
                )
            top_record_result = _plan_top_airtable_record_from_schema(
                text=active_text,
                normalized=normalized,
                tables=schema_tables,
                live=live,
            )
            if top_record_result is not None:
                return top_record_result
            aggregate_result = _plan_airtable_aggregate_from_schema(
                text=active_text,
                normalized=normalized,
                tables=schema_tables,
                live=live,
            )
            if aggregate_result is not None:
                return aggregate_result

        if table:
            read = airtable_read_records_impl(
                table,
                base_alias="finance_tax_tracker",
                max_records=1,
                live=live,
            )
            records = read.get("records", []) if isinstance(read, Mapping) else []
            return _summarize_table_read(
                text=text,
                table=table,
                read=read,
                records=records,
            )

        if "operating note" in normalized or "approval gates" in normalized:
            summary = (
                "2026 Finance & Tax Tracker operating note: CoS can read schema and capped records "
                "from Business Income, Business Expenses, Personal Income, Personal Expenses, and "
                "the schema-resolved tax table. It can create/update records only through "
                "typed Airtable tools, "
                "allowed tables, explicit live flags, exact fields, and an approval reference. "
                "It can upload one receipt/invoice attachment only for an approved expense "
                "record after schema and record identity are known. It cannot delete, change "
                "schema, file returns, make payments, or give final tax/legal advice. Federal, "
                "Pennsylvania, and Philadelphia tax outputs are operational support notes; "
                "uncertain deductions, "
                "mixed-use expenses, entity-structure questions, estimated payments, "
                "and Philadelphia BIRT/NPT issues need human tax review."
            )
            return _finance_tracker_result(
                text=text,
                summary=summary,
                command="Produce finance tracker operating note",
                actions=[
                    "Read schema first.",
                    "Run dry-run write preview with exact fields.",
                    (
                        "Perform one approved live write only after write gates "
                        "and approval reference are present."
                    ),
                ],
                audit_notes=["No Airtable mutation was attempted for the operating note."],
            )

        if schema is None:
            schema = airtable_get_base_schema_impl(base_alias="finance_tax_tracker", live=live)
        tables = schema.get("schema", {}).get("tables", []) if isinstance(schema, Mapping) else []
        table_summaries = [
            f"{table['name']}: {len(table.get('fields', []))} fields"
            for table in tables
            if isinstance(table, Mapping)
        ]
        summary = (
            "Finance tracker schema: "
            f"{'; '.join(table_summaries) if table_summaries else 'no tables returned'}."
        )
        return _finance_tracker_result(
            text=text,
            summary=summary,
            command="Inspect finance_tax_tracker Airtable schema",
            actions=[
                "Use the schema summary before reads or writes.",
                "Keep memory/docs to schema and rules, not raw transaction history.",
            ],
            audit_notes=["Airtable metadata was reduced to bounded table and field counts."],
        )
    except Exception as exc:
        return _finance_tracker_result(
            text=text,
            summary=(
                "Finance tracker deterministic handler could not complete: "
                f"{type(exc).__name__}: {exc}"
            ),
            command=command,
            actions=[
                "Check Airtable base id/token and allowed-table config.",
                "Retry after connector credentials and network access are verified.",
            ],
            audit_notes=["Finance tracker handler returned a controlled failure summary."],
        )


def _extract_target_channel(text: str, fallback: str) -> str:
    match = re.search(r"#([a-z0-9_-]+)", text, flags=re.I)
    if match:
        return match.group(1)
    return fallback


def _extract_target_channels(text: str, fallback: str = "") -> list[str]:
    channels: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"#([a-z0-9_-]+)", text, flags=re.I):
        channel = match.group(1).strip().lower()
        if channel and channel not in seen:
            channels.append(channel)
            seen.add(channel)
    if not channels and fallback:
        channels.append(fallback.strip().lstrip("#"))
    return [channel for channel in channels if channel]


def _extract_time_window(text: str) -> str:
    lowered = text.lower()
    if "since last" in lowered:
        return "since_last_summary"
    if "today" in lowered or "day end" in lowered or "end of day" in lowered:
        return "today"
    if "yesterday" in lowered:
        return "yesterday"
    if "this week" in lowered or "weekly" in lowered:
        return "this_week"
    if "last week" in lowered:
        return "last_week"
    if "morning" in lowered:
        return "morning"
    if "afternoon" in lowered:
        return "afternoon"
    return "recent"


def _capabilities_for_request(text: str) -> list[str]:
    lowered = text.lower()
    capabilities: list[str] = []
    if any(
        term in lowered
        for term in ("summarize", "summary", "brief", "catch me up", "review activity")
    ):
        capabilities.append("channel_summary")
    if any(
        term in lowered for term in ("project", "context pack", "what do we know", "obtain context")
    ):
        capabilities.append("project_context")
    if any(
        term in lowered
        for term in ("research plan", "research direction", "opportunity lane", "stakeholder")
    ):
        capabilities.append("research_direction")
    if len(_extract_target_channels(text)) > 1 or "cross-channel" in lowered or "across" in lowered:
        capabilities.append("cross_channel_synthesis")
    if any(term in lowered for term in ("article", "articles", "link", "links", "url")):
        capabilities.append("article_link_review")
    if explicit_full_article_read_requested(text):
        capabilities.append("full_article_reading")
    if any(term in lowered for term in ("doc", "docs", "document", "proposal", "deck")):
        capabilities.append("document_review")
    if any(
        term in lowered for term in ("meeting prep", "prepare me for", "agenda", "talking points")
    ):
        capabilities.append("meeting_prep")
    if any(
        term in lowered for term in ("follow-up", "follow up", "stale", "unresolved", "open loop")
    ):
        capabilities.append("follow_up_tracking")
    if any(
        term in lowered
        for term in (
            "airtable",
            "google drive",
            "google doc",
            "google docs",
            "google sheet",
            "google sheets",
            "spreadsheet",
            "structured data",
            "knio",
            "artifact",
            "contact",
            "company info",
        )
    ):
        capabilities.append("artifact_write_planning")
    if any(term in lowered for term in ("google sheet", "google sheets", "spreadsheet")):
        capabilities.append("google_sheets_structured_data")
    if any(term in lowered for term in ("budget", "cost", "spend")):
        capabilities.append("budget_aware_execution")
    if any(
        term in lowered
        for term in ("portfolio", "priorities", "blocked projects", "stale opportunities")
    ) or ("weekly executive summary" in lowered and "project" in lowered):
        capabilities.append("portfolio_oversight")
    if any(
        term in lowered
        for term in ("draft an email", "write an email", "compose an email", "intro email")
    ):
        capabilities.append("outreach_drafting")
    return capabilities or ["workflow_routing"]


def _extract_digest_field(text: str, field: str) -> str:
    match = re.search(rf"^{re.escape(field)}:\s*(.+)$", text, flags=re.I | re.M)
    return match.group(1).strip() if match else ""


def _looks_like_slack_history_digest_request(text: str) -> bool:
    return SLACK_HISTORY_DIGEST_MARKER in text


def _slack_history_items_from_digest(text: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        match = SLACK_HISTORY_ITEM_RE.match(line.strip())
        if not match:
            continue
        body = re.sub(r"https?://\S+", "", match.group("body")).strip()
        title = (match.group("title") or "").strip()
        if title == "Slack message":
            title = ""
        if not title:
            title = body.split(".", 1)[0].strip()[:120]
        lowered = f"{title} {body}".lower()
        if "chief of staff" in lowered and any(
            marker in lowered for marker in ("summarize activity", "review activity", "catch me up")
        ):
            continue
        if "has joined the channel" in lowered:
            continue
        topic = _slack_history_topic_from_text(title, body)
        metadata = _slack_history_metadata_from_text(body)
        items.append(
            {
                "ts": match.group("ts").strip(),
                "topic": topic,
                "metadata": metadata,
            }
        )
    return items


def _slack_history_topic_from_text(title: str, body: str) -> str:
    topic = ""
    for pattern in (
        r"watchlist generated for [`'\"]*([^`'\".]+)",
        r"\*Query:\*\s*([^*\n]+)",
        r"\bQuery:\s*([^*\n]+)",
        r"\bTopics?:\s*([^|.\n]+(?:\|[^|.\n]+)*)",
        r"\bSummary:\s*([^*\n]+)",
    ):
        match = re.search(pattern, body, flags=re.I)
        if match:
            topic = match.group(1).strip(" `\"'.:")
            break
    if not topic:
        topic = title.strip(" `\"'.:")
    topic = re.sub(r"\*+", "", topic)
    topic = re.sub(r"\bMatched on:.*", "", topic, flags=re.I)
    topic = re.sub(r"\bLink:.*", "", topic, flags=re.I)
    topic = re.sub(r"\bSummary:.*", "", topic, flags=re.I)
    topic = re.sub(r"\s+", " ", topic).strip()
    topic = re.sub(r"\s+\|", " |", topic)
    return _truncate_sentence(topic or "Slack post", 120)


def _slack_history_metadata_from_text(body: str) -> str:
    metadata: list[str] = []
    for label, pattern in (
        ("workflow", r"\*?Workflow:\*?\s*`?([A-Za-z0-9_-]+)`?"),
        ("status", r"\*?Status:\*?\s*`?([A-Za-z0-9_-]+)`?"),
    ):
        match = re.search(pattern, body, flags=re.I)
        if match:
            metadata.append(f"{label}={match.group(1).strip()}")
    return "; ".join(metadata)


def _format_slack_history_timestamp(ts: str) -> str:
    try:
        posted_at = datetime.fromtimestamp(float(ts), ZoneInfo("America/New_York"))
    except (OverflowError, ValueError):
        return f"Slack ts {ts}"
    return posted_at.strftime("%Y-%m-%d %H:%M %Z")


def _truncate_sentence(text: str, limit: int) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _slack_history_theme(items: list[dict[str, str]]) -> str:
    combined = " ".join(f"{item['topic']} {item['metadata']}" for item in items).lower()
    if "clinicaltrials" in combined or "clinical trials" in combined:
        return (
            "ClinicalTrials.gov watchlist posts around mood, psychosis, AI/ML, "
            "depression, bipolar, ketamine/esketamine, schizophrenia, and early psychosis."
        )
    if any(marker in combined for marker in ("grant", "funding", "nih", "rfa", "foa")):
        return "Funding and grant opportunity updates with emphasis on the linked source material."
    if any(marker in combined for marker in ("paper", "preprint", "pubmed", "article")):
        return "Research article and publication updates from the linked posts."
    return "Recent linked Slack posts from the supplied channel-history digest."


def _plan_slack_history_digest_request(text: str) -> ChiefOfStaffResult:
    channel = _extract_digest_field(text, "Channel")
    channel_id = _extract_digest_field(text, "Channel id")
    items = _slack_history_items_from_digest(text)
    summary_lines = ["Summary:"]
    if items:
        topics = ", ".join(item["topic"] for item in items[:4])
        summary_lines.append(f"The supplied channel history centers on {topics}.")
        summary_lines.append("")
        summary_lines.append("Useful follow-ups:")
        for item in items[:3]:
            detail = f" ({item['metadata']})" if item["metadata"] else ""
            summary_lines.append(
                f"- Review `{item['topic']}` from "
                f"{_format_slack_history_timestamp(item['ts'])}{detail}."
            )
    else:
        summary_lines.append(
            "No candidate Slack history items were present in the supplied digest."
        )
    summary_lines.append("")
    summary_lines.append(f"Theme: {_slack_history_theme(items)}")
    summary = "\n".join(summary_lines)
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text.split(SLACK_HISTORY_DIGEST_MARKER, 1)[0].strip() or text[:240],
        summary=summary,
        time_window=_extract_time_window(text),
        target_channels=_extract_target_channels(text, channel.lstrip("#") or channel_id),
        operating_capabilities=_capabilities_for_request(text),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="slack-runtime-review",
            command_text=f"Summarize supplied Slack history digest for {channel_id or channel}",
            target_channel=(channel.lstrip("#") or channel_id or "selected-channel"),
            rationale=(
                "A bounded Slack message-history digest was supplied by the KNI Slack runtime."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            "Use the supplied Slack history digest as evidence for a concise channel summary.",
            "Ignore the request message and avoid run metadata in Slack-facing output.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            ChiefOfStaffSourceRef(
                title="Supplied Slack message-history digest",
                source_type="slack_runtime_digest",
                note=f"Channel {channel_id or channel}; bounded read-only context.",
            )
        ],
        context_sources_considered=[
            "supplied_slack_message_history_digest",
            "operator_and_agent_policy",
            "keystone_slack_runtime_repo",
        ],
        repo_context_used=_repo_context_for_capability("slack-runtime-review"),
        audit_notes=[
            "Deterministic Slack history digest renderer used.",
            "No model synthesis was needed for the bounded read-only channel summary.",
            "No Slack, Gmail, Calendar, CRM, or repo write was attempted.",
        ],
    )


def _repo_context_for_capability(workflow_type: str) -> list[str]:
    common = [
        "keystone-slack:AGENTS.md",
        "keystone-slack:slack/kni-app-manifest.yaml",
        "keystone-slack:kni_integrations/slack_socket_mode.py",
        "keystone-slack:kni_integrations/workflow_runner.py",
        "keystone-slack:kni_integrations/business_agents_bridge.py",
    ]
    if workflow_type == "calendar-read":
        return [*common, "keystone-slack:kni_integrations/workflow_family_calendar.py"]
    if workflow_type in {"gmail-summary", "gmail-triage"}:
        return [*common, "keystone-slack:kni_integrations/workflow_family_gmail.py"]
    return common


def _action_lines(capability: Mapping[str, Any], target_channel: str) -> list[str]:
    command = str(capability.get("command_text") or "").strip()
    workflow = str(capability.get("workflow_type") or "clarification").strip()
    notes = [str(note).strip() for note in capability.get("notes") or [] if str(note).strip()]
    if workflow == "slack-runtime-review":
        return notes or [
            "Plan KNI Slack operations and recommend safe existing commands.",
            "Use read-only context only unless a live gated source is explicitly enabled.",
            "Keep all Slack posts, Gmail sends, calendar writes, and repo writes blocked.",
        ]
    lines = [
        f"Use existing KNI Slack workflow `{command}` as the starting point.",
        f"Treat #{target_channel} as the intended review channel, not as approval to post.",
        "Review the generated Slack copy before any public channel post.",
    ]
    if workflow == "calendar-read":
        lines.append("Use a read-only calendar brief; do not create or update calendar events.")
    elif workflow in {"gmail-summary", "gmail-triage"}:
        lines.append("Use Gmail summary or triage only; do not send replies from this agent.")
    elif workflow == "business-agents-route":
        lines.append(
            "Delegate company, opportunity, triage, or outreach work to Keystone Business Agents."
        )
    else:
        lines.append("Ask for the source, window, and target Slack channel before routing.")
    return lines


def _looks_like_reference_capture_request(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "keep this for future reference",
        "for future reference",
        "remember this",
        "save this",
        "save for later",
        "bookmark this",
        "note this",
        "store this",
        "add this to memory",
    )
    if any(marker in lowered for marker in markers):
        return True
    return bool(URL_PATTERN.search(text)) and any(
        marker in lowered for marker in ("remember", "reference", "bookmark", "save")
    )


def _looks_like_chief_memory_capture_request(text: str) -> bool:
    lowered = text.lower()
    has_write = any(
        marker in lowered
        for marker in (
            "remember this as",
            "save this as",
            "store this as",
            "mark this as",
            "update the direction",
            "update project aim",
            "save this decision",
        )
    )
    has_memory_kind = any(
        marker in lowered
        for marker in (
            "goal",
            "aim",
            "constraint",
            "decision",
            "priority",
            "budget",
            "strategy",
            "direction",
            "status",
            "avoid",
            "do not",
        )
    )
    return has_write and has_memory_kind


def _looks_like_chief_memory_review_request(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "show strategic memory",
            "show chief of staff memory",
            "show memory for",
            "what do you remember",
            "what memory",
            "review memory for",
        )
    )


def _chief_memory_type_from_text(text: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in ("budget", "cost", "spend")):
        return "budget_assumption"
    if any(marker in lowered for marker in ("constraint", "requirement", "limitation")):
        return "project_constraint"
    if any(marker in lowered for marker in ("decision", "decided", "choice")):
        return "project_decision"
    if any(marker in lowered for marker in ("status", "snapshot", "current state")):
        return "project_status_snapshot"
    if any(marker in lowered for marker in ("priority", "focus", "portfolio")):
        return "portfolio_priority"
    if any(marker in lowered for marker in ("avoid", "do not", "don't", "never")):
        return "avoidance_rule"
    if any(marker in lowered for marker in ("strategy", "direction")):
        return "operator_strategy"
    return "project_goal"


PROJECT_KEY_RE = re.compile(
    r"\b(?:for|about|on|re:)\s+(?:project\s+)?(?P<key>[A-Za-z0-9][A-Za-z0-9 _-]{1,80})",
    flags=re.I,
)


def _extract_memory_object_key(text: str) -> str:
    match = PROJECT_KEY_RE.search(text)
    if not match:
        return ""
    key = match.group("key")
    key = re.split(
        r"\b(?:that|to|is|should|because|:|;|,|\.)\b",
        key,
        maxsplit=1,
        flags=re.I,
    )[0]
    return " ".join(key.split()).strip()


def _memory_title_from_text(text: str, memory_type: str, object_key: str) -> str:
    title = text
    title = re.sub(r"^@kni\b", " ", title, flags=re.I).strip()
    title = re.sub(r"\bchief\s+of\s+staff\b", " ", title, flags=re.I).strip()
    title = re.sub(
        r"\b(please\s+)?(remember|save|store|mark|update|keep|note)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\bas\s+a[n]?\s+\w+\b", " ", title, flags=re.I)
    title = re.sub(r"\bfor\s+project\s+", " for ", title, flags=re.I)
    title = re.sub(r"[:\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if title:
        return title[:160]
    suffix = f" for {object_key}" if object_key else ""
    return f"{memory_type.replace('_', ' ').title()}{suffix}"[:160]


def _extract_first_url(text: str) -> str:
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip(".,);]") if match else ""


def _reference_title_from_text(text: str, url: str) -> str:
    title = text
    if url:
        title = title.replace(url, " ")
    title = re.sub(r"^@kni\b", " ", title, flags=re.I).strip()
    title = re.sub(r"\bchief\s+of\s+staff\b", " ", title, flags=re.I).strip()
    title = re.sub(
        r"\b(please\s+)?(keep|remember|save|bookmark|note|store|add)\b",
        " ",
        title,
        flags=re.I,
    )
    title = re.sub(r"\b(this|for|future|reference|later|to|memory)\b", " ", title, flags=re.I)
    title = re.sub(r"[:\-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    if title:
        return title[:160]
    if url:
        return url
    return "Operator reference"


def _plan_reference_capture(text: str, *, database_url: str | None = None) -> ChiefOfStaffResult:
    url = _extract_first_url(text)
    title = _reference_title_from_text(text, url)
    summary = f"Operator asked Chief of Staff to keep this reference for future use: {title}."
    store = SQLiteStore(database_url or database_url_from_env())
    memory_item = operator_reference_memory_item(
        title=title,
        summary=summary,
        url=url,
        request_text=text,
        source="chief_of_staff_natural_language",
    )
    memory_id = store.save_memory_item(memory_item)
    artifact = AutomationArtifactRef(
        artifact_id=f"memory:{memory_id}",
        artifact_type="operator_reference_memory",
        title=title,
        url=url,
        provider="sqlite",
        dry_run=False,
        metadata={
            "memory_id": memory_id,
            "memory_type": "operator_reference",
            "operator": "Anup",
            "source": "chief_of_staff_natural_language",
        },
    )
    saved_location = f"Keystone memory `memory:{memory_id}`"
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=f"Saved reference for future use: {title}. Saved to {saved_location}.",
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="reference-capture",
            command_text="@KNI chief of staff keep this for future reference",
            target_channel=_extract_target_channel(text, "current-thread"),
            rationale=(
                "Anup explicitly asked Chief of Staff to retain an internal reference. "
                "The reference was stored as prompt-safe Keystone memory."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            f"Saved to {saved_location}.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            ChiefOfStaffSourceRef(
                title=title,
                url=url,
                source_type="operator_reference",
                note="Operator-supplied reference captured for internal future use.",
            )
        ],
        context_sources_considered=[
            "operator_identity:Anup",
            "operator_and_agent_policy",
            "keystone_memory",
            "keystone_local_context",
        ],
        artifact_refs=[artifact],
        audit_notes=[
            "Deterministic Chief of Staff natural-language intent matched reference capture.",
            "Reference capture is scoped to founder/operator use by Anup.",
            (
                "No live web fetch was attempted; the operator-supplied URL was stored "
                "as a source reference."
            ),
        ],
    )


def _plan_chief_memory_capture(text: str, *, database_url: str | None = None) -> ChiefOfStaffResult:
    memory_type = _chief_memory_type_from_text(text)
    object_key = _extract_memory_object_key(text)
    title = _memory_title_from_text(text, memory_type, object_key)
    summary = f"Operator-supplied Chief of Staff {memory_type.replace('_', ' ')}: {title}."
    store = SQLiteStore(database_url or database_url_from_env())
    memory_item = chief_of_staff_memory_item(
        memory_type=memory_type,
        title=title,
        summary=summary,
        object_id=object_key or title,
        object_key=object_key or title,
        content={
            "request_text": text[:500],
            "object_key": object_key or title,
            "capture_mode": "natural_language",
        },
        source_ids=["operator_supplied_chief_of_staff_memory"],
        confidence=0.75,
    )
    memory_id = store.save_memory_item(memory_item)
    artifact = AutomationArtifactRef(
        artifact_id=f"memory:{memory_id}",
        artifact_type="chief_of_staff_memory",
        title=title,
        provider="sqlite",
        dry_run=False,
        metadata={
            "memory_id": memory_id,
            "memory_type": memory_type,
            "object_key": object_key or title,
            "operator": "Anup",
        },
    )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=(f"Saved Chief of Staff {memory_type.replace('_', ' ')} memory: {title}."),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="reference-capture",
            command_text="@KNI chief of staff remember this as strategic memory",
            target_channel=_extract_target_channel(text, "current-thread"),
            rationale=(
                "Anup explicitly supplied durable strategic memory. It was stored as "
                "approved prompt-safe Keystone memory and remains internal."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            f"Saved to Keystone memory `memory:{memory_id}`.",
            "Use this memory only as internal guidance unless separately approved.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        context_sources_considered=[
            "operator_identity:Anup",
            "operator_and_agent_policy",
            "keystone_memory",
        ],
        artifact_refs=[artifact],
        audit_notes=[
            "Deterministic Chief of Staff strategic memory capture used.",
            "No external write or live connector was attempted.",
        ],
    )


def _plan_chief_memory_review(text: str, *, database_url: str | None = None) -> ChiefOfStaffResult:
    object_key = _extract_memory_object_key(text)
    memory_context = build_chief_of_staff_memory_context(
        query=text,
        route="memory-review",
        object_key=object_key or None,
        database_url=database_url or database_url_from_env(),
    )
    count = len(memory_context.records)
    summary = (
        f"Found {count} approved Chief of Staff memory item{'s' if count != 1 else ''}."
        if count
        else memory_context.missing_reason
    )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=summary,
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="memory-review",
            command_text="@KNI chief of staff show strategic memory",
            target_channel=_extract_target_channel(text, "current-thread"),
            rationale="Memory review is read-only and uses approved prompt-safe records only.",
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=_memory_action_lines(memory_context),
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=_memory_source_refs(memory_context.records),
        context_sources_considered=[
            "operator_and_agent_policy",
            "keystone_memory",
        ],
        memory_context=memory_context,
        audit_notes=[
            "Deterministic Chief of Staff memory review used.",
            "Unapproved, unsafe, expired, and superseded memory is excluded.",
        ],
    )


def _memory_action_lines(memory_context: Any) -> list[str]:
    if not getattr(memory_context, "records", None):
        return [
            "Proceed with a validation plan instead of assuming durable context exists.",
            (
                "Ask Anup to save project goals, constraints, decisions, or budget "
                "assumptions when known."
            ),
        ]
    return [f"Use approved memory: {item.title}." for item in memory_context.records[:5]]


def _memory_source_refs(records: list[Any]) -> list[ChiefOfStaffSourceRef]:
    refs: list[ChiefOfStaffSourceRef] = []
    for item in records[:5]:
        refs.append(
            ChiefOfStaffSourceRef(
                title=item.title,
                url=next((source for source in item.source_ids if source.startswith("http")), ""),
                source_type=f"memory:{item.memory_type}",
                note=item.summary,
            )
        )
    return refs


def _looks_like_operator_supplied_synthesis_request(text: str) -> bool:
    """Identify a complete direct-answer ask over explicitly supplied material."""

    return looks_like_supplied_context_synthesis_request(text)


def _chief_direct_supplied_synthesis(
    request_text: str,
    manual_request_plan: ManualRequestPlan | None,
) -> bool:
    """Use plan semantics for live requests; keep phrase matching as fallback."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    if not authority.canonical:
        if authority.invalid:
            return False
        return _looks_like_operator_supplied_synthesis_request(request_text)
    return bool(
        manual_request_plan.target_agent == "chief_of_staff"
        and manual_request_plan.intent in {"route_request", "context_lookup"}
        and not manual_request_plan.workflow
        and manual_request_plan.provider_system == "unspecified"
        and not manual_request_plan.provider_operations
        and not manual_request_plan.requires_live_search
        and not manual_request_plan.requires_durable_state
        and manual_request_plan.ask_shape.prior_context_dependency
        in {"none", "selected_context", "unspecified"}
    )


def _chief_semantic_tools_required(
    manual_request_plan: ManualRequestPlan | None,
) -> bool:
    return bool(
        ExecutionIntentAuthority.from_value(manual_request_plan).canonical
        and manual_request_plan is not None
        and (
            manual_request_plan.provider_system != "unspecified"
            or manual_request_plan.provider_operations
            or manual_request_plan.workflow
        )
    )


def _operator_supplied_synthesis_items(text: str, *, desired_count: int) -> list[str]:
    """Extract explicit semicolon/newline facts for the deterministic safe fallback."""

    raw = str(text or "").strip()
    raw = re.sub(
        r"(?im)^\s*Operator-supplied Slack attachment local path:\s*\S+\s*$",
        "",
        raw,
    ).strip()
    if ":" not in raw:
        return []
    material = raw.split(":", 1)[1].strip()
    material = re.split(
        r"\n(?:Prior result for context|Authoritative follow-up):",
        material,
        maxsplit=1,
        flags=re.I,
    )[0]
    material = re.split(
        r"(?:^|[.!?]\s+)(?:do\s+not|don't|dont|never|no\s+web\s+search|"
        r"make\s+no\s+changes?|perform\s+no\s+changes?)\b",
        material,
        maxsplit=1,
        flags=re.I,
    )[0]
    candidates = re.split(r"\s*;\s*|\n\s*(?:[-*]\s*)?", material)
    items: list[str] = []
    for candidate in candidates:
        cleaned = " ".join(candidate.strip(" \t-*").split()).strip(" .")
        if not cleaned:
            continue
        cleaned = re.sub(r"^(?:and\s+)", "", cleaned, flags=re.I)
        cleaned = cleaned[0].upper() + cleaned[1:] if cleaned else cleaned
        if cleaned and cleaned[-1] not in ".!?":
            cleaned += "."
        if cleaned and cleaned not in items:
            items.append(cleaned)
    return items[: max(1, desired_count)]


def _plan_operator_supplied_synthesis_request(
    text: str,
    *,
    manual_request_plan: ManualRequestPlan,
) -> ChiefOfStaffResult | None:
    """Return a useful read-only fallback instead of an unrelated Slack route."""

    semantic_supplied_context = _chief_direct_supplied_synthesis(
        text,
        manual_request_plan,
    )
    if not semantic_supplied_context:
        return None
    items = _operator_supplied_synthesis_items(
        text,
        desired_count=manual_request_plan.desired_count,
    )
    if not items:
        return None
    summary = "\n".join(f"- {item}" for item in items)
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=summary,
        synthesis=summary,
        target_channels=["current-thread"],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="project-context-review",
            command_text="",
            target_channel="current-thread",
            rationale=(
                "The operator supplied the complete evidence boundary and requested "
                "a read-only direct answer."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[],
        context_sources_considered=["operator_supplied_context"],
        repo_context_used=[],
        audit_notes=[
            "Deterministic supplied-context fallback used after preserving the requested shape.",
            "No search, provider read, provider write, Slack post, or external action was attempted.",
        ],
    )


def _plan_natural_language_operating_intent(
    text: str, *, database_url: str | None = None
) -> ChiefOfStaffResult | None:
    lowered = text.lower()
    if _looks_like_internal_handoff_request(lowered):
        command_text = _internal_handoff_command_text(lowered)
        durable_handoff = _internal_handoff_durable_handoff(command_text)
        return _natural_language_intent_result(
            text,
            workflow_type="research-direction-review",
            command_text=command_text,
            target_channel=_extract_target_channel(text, "current-thread"),
            summary=(
                "Prepare an internal handoff that selects the next owner, explains why the "
                "route fits, names the minimum context needed before committing, and keeps "
                "external action blocked."
            ),
            rationale=(
                "Internal review handoffs should route to the best next specialist without "
                "drafting outreach or using live context sources unless explicitly approved."
            ),
            recommended_actions=[
                "Name the next owner or specialist route using Chief of Staff -> Agent notation.",
                "Ask for the dashboard schema, metric definitions, intended users, and review timeline.",
                "Keep outreach, writes, live source access, and external commitments blocked.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "business_workflow_state",
                "keystone_local_context",
            ],
            requires_live_connector=False,
            database_url=database_url,
            durable_handoff=durable_handoff,
            context_handoffs=_internal_context_handoffs(
                lowered,
                durable_handoff=durable_handoff,
            ),
        )
    if _looks_like_outreach_drafting_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="business-agents-route",
            command_text="@KNI outreach composer <draft-only outreach request>",
            target_channel=_extract_target_channel(text, "ai-agents-workflow"),
            summary=(
                "Route the request to Outreach Composer for draft-only outbound copy "
                "using approved context."
            ),
            rationale="Outbound copy must remain draft-only, source-backed, and approval-gated.",
            recommended_actions=[
                "Confirm the recipient, objective, and approved source-backed claims.",
                "Delegate drafting to Outreach Composer or Gmail Triage for a selected thread.",
                "Hold any send, publish, or Gmail draft creation behind human approval.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "business_workflow_state",
                "keystone_local_context",
                "selected_gmail_context",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    if _looks_like_budget_resource_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="budget-resource-review",
            command_text="@KNI chief of staff review known budget and resource context",
            target_channel=_extract_target_channel(text, "current-thread"),
            summary=(
                "Answer from known budget/resource records only; otherwise provide "
                "assumptions and a validation plan."
            ),
            rationale="Budget answers must not invent numbers or imply unverified commitments.",
            recommended_actions=[
                "Check approved records, WorkItems, artifacts, and operator-supplied context.",
                "Separate known amounts from estimates and assumptions.",
                "List the minimum records needed to validate any unknown budget or resource plan.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "business_workflow_state",
                "keystone_local_context",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    if _looks_like_meeting_prep_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="meeting-prep",
            command_text="@KNI chief of staff prepare meeting brief",
            target_channel=_extract_target_channel(text, "current-thread"),
            summary=(
                "Prepare a meeting brief from selected project, Slack, Gmail, calendar, "
                "and local context without changing the calendar."
            ),
            rationale="Meeting prep is read-only and may include draft follow-up copy only.",
            recommended_actions=[
                "Identify the meeting target, time window, and project or company context.",
                "Prepare agenda, talking points, questions, and open risks.",
                "Draft follow-up text only when requested and keep it approval-gated.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "keystone_local_context",
                "selected_calendar_context",
                "selected_gmail_context",
                "keystone_slack_runtime_repo",
            ],
            requires_live_connector=True,
            database_url=database_url,
        )
    if _looks_like_document_review_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="slack-docs-review",
            command_text="@KNI chief of staff review supplied docs or approved local context",
            target_channel=_extract_target_channel(text, "current-thread"),
            summary=(
                "Review supplied or approved documents for decisions, risks, claims, "
                "missing evidence, and next actions."
            ),
            rationale=(
                "Document review can summarize internal context but external claims need approval."
            ),
            recommended_actions=[
                "Use supplied files or allowlisted local context as the evidence base.",
                "Extract decisions, risks, reusable claims, missing evidence, and owners.",
                "Mark any externally reusable claim as requiring source review and approval.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "keystone_local_context",
                "official_developer_docs",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    if _looks_like_portfolio_review_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="portfolio-review",
            command_text="@KNI chief of staff summarize behavioral health project portfolio",
            target_channel=_extract_target_channel(text, "ai-agents-workflow"),
            summary=(
                "Summarize active behavioral health project priorities, blockers, stale "
                "items, approvals, and next actions."
            ),
            rationale=(
                "Portfolio review is a summary and prioritization task unless a write is approved."
            ),
            recommended_actions=[
                "Inspect WorkItems, approvals, artifacts, memory, and selected Slack context.",
                (
                    "Rank current priorities and separate blocked, stale, speculative, "
                    "and active work."
                ),
                "Recommend what to continue, pause, or escalate.",
            ],
            context_sources=[
                "operator_and_agent_policy",
                "business_workflow_state",
                "keystone_memory",
                "keystone_local_context",
                "keystone_slack_runtime_repo",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    if _looks_like_project_context_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="project-context-review",
            command_text="@KNI chief of staff build project context brief",
            target_channel=_extract_target_channel(text, "current-thread"),
            summary=(
                "Build a project context brief from approved local state and selected "
                "operating context."
            ),
            rationale=(
                "Project context should identify known facts, gaps, blockers, and next actions."
            ),
            recommended_actions=[
                (
                    "Gather WorkItems, memory, local context, and selected "
                    "Slack/Gmail/Calendar summaries."
                ),
                "Summarize what is known, what is missing, and which facts need validation.",
                (
                    "Recommend the next owner or specialist handoff if research, "
                    "outreach, or triage is needed."
                ),
            ],
            context_sources=[
                "operator_and_agent_policy",
                "business_workflow_state",
                "keystone_memory",
                "keystone_local_context",
                "selected_gmail_context",
                "selected_calendar_context",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    if _looks_like_research_direction_request(lowered):
        return _natural_language_intent_result(
            text,
            workflow_type="research-direction-review",
            command_text="@KNI business research analyst <research direction request>",
            target_channel=_extract_target_channel(text, "ai-agents-workflow"),
            summary=(
                "Route the idea into source-backed research questions, opportunity lanes, "
                "and specialist handoff."
            ),
            rationale="Research direction needs source-backed synthesis before prioritization.",
            recommended_actions=[
                "Clarify the behavioral health topic, target population, and business objective.",
                (
                    "Delegate source-backed synthesis to Business Research Analyst "
                    "when facts are needed."
                ),
                (
                    "Delegate opportunity discovery to Opportunity Scout when the "
                    "goal is leads, grants, partners, or companies."
                ),
            ],
            context_sources=[
                "operator_and_agent_policy",
                "keystone_local_context",
                "business_workflow_state",
            ],
            requires_live_connector=False,
            database_url=database_url,
        )
    return None


def _natural_language_intent_result(
    text: str,
    *,
    workflow_type: str,
    command_text: str,
    target_channel: str,
    summary: str,
    rationale: str,
    recommended_actions: list[str],
    context_sources: list[str],
    requires_live_connector: bool,
    database_url: str | None = None,
    durable_handoff: ChiefDurableHandoff | None = None,
    context_handoffs: list[ChiefContextHandoff] | None = None,
) -> ChiefOfStaffResult:
    memory_context = build_chief_of_staff_memory_context(
        query=text,
        route=workflow_type,
        object_key=_extract_memory_object_key(text) or None,
        database_url=database_url or database_url_from_env(),
    )
    memory_actions = _memory_action_lines(memory_context)
    memory_audit_note = (
        f"Retrieved {len(memory_context.records)} approved Chief of Staff memory item(s)."
        if memory_context.records
        else memory_context.missing_reason
    )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=summary,
        time_window=_extract_time_window(text),
        target_channels=_extract_target_channels(text, target_channel),
        operating_capabilities=_capabilities_for_request(text),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type=workflow_type,  # type: ignore[arg-type]
            command_text=command_text,
            target_channel=target_channel,
            rationale=rationale,
            requires_live_connector=requires_live_connector,
            requires_human_approval_before_post=True,
        ),
        durable_handoff=durable_handoff,
        context_handoffs=context_handoffs or [],
        recommended_actions=[
            *recommended_actions,
            *memory_actions,
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            *_docs_for_topic(text),
            *_memory_source_refs(memory_context.records),
        ],
        context_sources_considered=list(dict.fromkeys([*context_sources, "keystone_memory"])),
        repo_context_used=_repo_context_for_capability(workflow_type),
        memory_context=memory_context,
        audit_notes=[
            "Deterministic Chief of Staff natural-language intent routing used.",
            "Intent matrix selected a flexible route family, not a fixed command grammar.",
            memory_audit_note,
            "No Slack, Gmail, Calendar, CRM, LinkedIn, or repo write was attempted.",
        ],
    )


def _looks_like_outreach_drafting_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "write an email",
            "draft an email",
            "compose an email",
            "email to",
            "intro email",
            "linkedin note",
            "draft a reply",
        )
    )


def _looks_like_internal_handoff_request(lowered: str) -> bool:
    if not any(
        marker in lowered
        for marker in (
            "internal handoff",
            "best next owner",
            "best next specialist",
            "best next workitem-capable specialist",
            "best next work item-capable specialist",
            "next owner or agent",
            "workitem-capable specialist",
            "work item-capable specialist",
            "hand off if appropriate",
            "recommended next path",
            "what remains blocked",
            "information keystone should request",
            "before committing",
        )
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "business research agent",
            "airtable context agent",
            "handoff",
            "owner",
            "agent",
            "review",
        )
    )


def _internal_handoff_command_text(lowered: str) -> str:
    if "gmail triage agent" in lowered or "gmail inbound triage" in lowered:
        return "Chief of Staff -> Gmail Triage Agent"
    if "outreach composer agent" in lowered:
        return "Chief of Staff -> Outreach Composer Agent"
    if "opportunity scout agent" in lowered:
        return "Chief of Staff -> Opportunity Scout Agent"
    if (
        "google workspace context agent" in lowered
        or "google drive context agent" in lowered
    ):
        return "Chief of Staff -> Google Workspace Context Agent"
    if "airtable context agent" in lowered and "business research agent" not in lowered:
        return "Chief of Staff -> Airtable Context Agent"
    return "Chief of Staff -> Business Research Agent"


def _internal_handoff_durable_handoff(command_text: str) -> ChiefDurableHandoff | None:
    handoff_by_command: dict[str, ChiefDurableHandoffAgent] = {
        "Chief of Staff -> Gmail Triage Agent": "gmail_triage",
        "Chief of Staff -> Outreach Composer Agent": "outreach_composer",
        "Chief of Staff -> Opportunity Scout Agent": "opportunity_scout",
        "Chief of Staff -> Business Research Agent": "business_research_analyst",
    }
    agent = handoff_by_command.get(command_text)
    if agent is None:
        return None
    return ChiefDurableHandoff(
        agent=agent,
        rationale=(
            "Deterministic Chief of Staff selected this WorkItem-capable specialist "
            "as the durable next owner for the internal handoff."
        ),
    )


def _internal_context_handoffs(
    lowered: str,
    *,
    durable_handoff: ChiefDurableHandoff | None,
) -> list[ChiefContextHandoff]:
    handoffs: list[ChiefContextHandoff] = []
    if "rss context agent" in lowered or "announcements context agent" in lowered:
        handoffs.append(
            _internal_context_handoff(
                "rss_context_agent",
                durable_handoff=durable_handoff,
            )
        )
    if "preprints context agent" in lowered or "preprint context agent" in lowered:
        handoffs.append(
            _internal_context_handoff(
                "preprints_context_agent",
                durable_handoff=durable_handoff,
            )
        )
    if "zotero context agent" in lowered:
        handoffs.append(
            _internal_context_handoff(
                "zotero_context_agent",
                durable_handoff=durable_handoff,
            )
        )
    if "airtable context agent" in lowered:
        handoffs.append(
            _internal_context_handoff(
                "airtable_context_agent",
                durable_handoff=durable_handoff,
            )
        )
    if (
        "google workspace context agent" in lowered
        or "google drive context agent" in lowered
    ):
        handoffs.append(
            _internal_context_handoff(
                "google_workspace_context_agent",
                durable_handoff=durable_handoff,
            )
        )
    return handoffs


def _internal_context_handoff(
    agent: ChiefContextHandoffAgent,
    *,
    durable_handoff: ChiefDurableHandoff | None,
) -> ChiefContextHandoff:
    return ChiefContextHandoff(
        agent=agent,
        before_agent=durable_handoff.agent if durable_handoff is not None else None,
        rationale=(
            "Deterministic Chief of Staff selected this context agent for read-only "
            "staging before any durable specialist or provider write."
        ),
    )


def _looks_like_budget_resource_request(lowered: str) -> bool:
    return any(marker in lowered for marker in ("budget", "cost", "spend", "resources"))


def _looks_like_meeting_prep_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "meeting prep",
            "prepare me for",
            "prep me for",
            "agenda for",
            "talking points",
            "post-meeting",
        )
    ) and any(marker in lowered for marker in ("meeting", "call", "agenda", "talking points"))


def _looks_like_document_review_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "review these docs",
            "review this doc",
            "review the docs",
            "review this proposal",
            "review the proposal",
            "review this deck",
            "extract decisions",
            "extract risks",
            "extract claims",
        )
    )


def _looks_like_portfolio_review_request(lowered: str) -> bool:
    unscoped_attention_request = bool(
        re.search(
            r"\bwhat\s+(?:needs?|requires?)\s+(?:my\s+)?attention(?:\s+(?:today|now))?\b",
            lowered,
        )
    ) and not re.search(
        r"\b(?:finance|tax|gmail|email|inbox|airtable|calendar|slack|work\s*items?)\b",
        lowered,
    )
    return unscoped_attention_request or any(
        marker in lowered
        for marker in (
            "portfolio",
            "top priorities",
            "this week priorities",
            "weekly executive summary",
            "blocked projects",
            "stale opportunities",
            "what should i focus on",
        )
    )


def _looks_like_project_context_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "obtain context",
            "project context",
            "context pack",
            "what do we know about",
            "summarize what we know",
            "current status of project",
            "project status",
        )
    )


def _looks_like_research_direction_request(lowered: str) -> bool:
    return any(
        marker in lowered
        for marker in (
            "research plan",
            "research direction",
            "turn this idea into",
            "opportunity lanes",
            "commercialization paths",
            "key stakeholders",
        )
    )


def _plan_web_search_brief_request(text: str) -> ChiefOfStaffResult:
    lowered = text.lower()
    topic = text.strip().strip('"') or "requested web research topic"
    summary = (
        "This is a read-only source-backed web brief request. Live retrieval should "
        "produce the answer with visible source URLs; deterministic fallback cannot "
        "verify current external facts."
    )
    actions = [
        "Run the live Chief of Staff or Business Research Analyst path with web search enabled.",
        "Keep the main answer as Answer and Detailed Summary, with provider details only in Metadata.",
        "Do not post, send, publish, schedule, or write externally from this fallback.",
    ]
    if "not a diagnostics" in lowered or "not a diagnostic" in lowered:
        actions.append("Do not render this as a diagnostics report unless explicitly re-requested.")
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=summary,
        time_window=_extract_time_window(text),
        target_channels=[],
        operating_capabilities=[
            "source_research",
            "web_search_brief",
            "provider_metadata_reporting",
        ],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="research-direction-review",
            command_text=f"Read-only source-backed web brief: {topic[:180]}",
            target_channel="current-thread",
            rationale=(
                "The request asks for current source-backed web research. "
                "Use live retrieval and keep all provider diagnostics in trailing metadata."
            ),
            requires_live_connector=True,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=actions,
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=_docs_for_topic(text),
        context_sources_considered=[
            "operator_request",
            "manual_request_plan",
            "shared_search_provider_policy",
        ],
        repo_context_used=_repo_context_for_capability("research-direction-review"),
        audit_notes=[
            "Deterministic Chief of Staff web-search brief fallback used.",
            "No live source facts were verified by this fallback.",
        ],
    )


def _plan_local_kni_document_lookup_request(text: str) -> ChiefOfStaffResult:
    packet = build_local_kni_evidence_packet_for_query(text, max_candidate_documents=5)
    diagnostics: dict[str, Any] = dict(packet.get("retrieval_diagnostics") or {})
    diagnostics.setdefault("local_only", True)
    diagnostics.setdefault("send_enabled", False)
    diagnostics["candidate_document_count"] = len(packet.get("candidate_documents") or [])
    candidate_documents = [
        doc
        for doc in list(packet.get("candidate_documents") or [])
        if isinstance(doc, Mapping)
    ]
    evidence_paths = [
        str(doc.get("relative_path") or "")
        for doc in candidate_documents
        if str(doc.get("relative_path") or "")
    ]
    lookup_kind = str(
        diagnostics.get("effective_lookup_kind")
        or diagnostics.get("lookup_kind")
        or "generic"
    )
    answer_focus = str(diagnostics.get("effective_answer_focus") or "")

    if not evidence_paths:
        status = str(diagnostics.get("search_status") or "unknown")
        summary = (
            "I could not find candidate local KNI document evidence for this question. "
            f"Search status: {status}. Refresh command: python3 -m kni_integrations.cli doc-index."
        )
        return ChiefOfStaffResult(
            mode="deterministic",
            intent=text[:500],
            summary=summary,
            synthesis=summary,
            operating_capabilities=["local_kni_document_search"],
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="project-context-review",
                command_text="@KNI chief of staff search local KNI documents",
                target_channel="current-thread",
                rationale=(
                    "The request asks for local KNI document evidence, not Slack workflow routing."
                ),
                requires_live_connector=False,
                requires_human_approval_before_post=True,
            ),
            recommended_actions=[
                "Refresh the local index if the expected document was recently added.",
                "Do not send, post, publish, or share local KNI document context externally.",
            ],
            blocked_side_effects=BLOCKED_SIDE_EFFECTS,
            approval_required=True,
            human_review_required=True,
            send_enabled=False,
            slack_post_allowed=False,
            sources=[],
            context_sources_considered=["local_kni_document_index"],
            retrieval_diagnostics=diagnostics,
            audit_notes=[
                "Query-driven local KNI document evidence lookup found no candidates.",
                "No external search or hosted vector store was used.",
            ],
        )

    path_list = "; ".join(evidence_paths[:3])
    focus_note = f" answer focus `{answer_focus}`" if answer_focus else ""
    summary = (
        "Local KNI document evidence is available for Chief of Staff synthesis; "
        f"lookup kind `{lookup_kind}`{focus_note}. Candidate evidence paths: {path_list}. "
        "This deterministic planner does not decide the substantive answer."
    )
    synthesis = (
        "Use the bounded local KNI evidence packet and guarded document tools for model "
        "synthesis. The model should interpret roles and dates from document text, report "
        "uncertainty, and include evidence paths. Local KNI context is read-only and not "
        "approval to send, post, publish, submit, or share externally."
    )
    review_reasons = sorted(
        {
            str(reason)
            for doc in candidate_documents
            for reason in list(doc.get("review_reasons") or [])
            if str(reason)
        }
    )
    diagnostics.update(
        {
            "lookup_kind": lookup_kind,
            "answer_focus": answer_focus,
            "evidence_paths": evidence_paths[:5],
            "review_required": any(bool(doc.get("review_required")) for doc in candidate_documents),
            "review_reasons": review_reasons,
        }
    )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text[:500],
        summary=summary,
        synthesis=synthesis,
        operating_capabilities=[
            "local_kni_document_search",
            "local_kni_document_read",
            "sensitive_context_guardrails",
        ],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="project-context-review",
            command_text="@KNI chief of staff search local KNI documents",
            target_channel="current-thread",
            rationale="The request asks for local KNI document evidence.",
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            "Use live Chief of Staff synthesis to interpret the bounded evidence packet before answering.",
            f"Review candidate evidence path: {evidence_paths[0]}.",
            "Do not send, post, publish, or share local KNI document context externally without explicit approval.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[
            ChiefOfStaffSourceRef(
                title=relative_path,
                url="",
                source_type="local_kni_document",
                note="Candidate local-only document evidence for model synthesis.",
            )
            for relative_path in evidence_paths[:5]
        ],
        context_sources_considered=["local_kni_document_index", "local_kni_document_file"],
        retrieval_diagnostics=diagnostics,
        audit_notes=[
            "Query-driven local KNI document evidence packet prepared; no one-off answer branch used.",
            "No external search or hosted vector store was used.",
            "Local document context remains read-only and send-disabled.",
        ],
    )


def plan_chief_of_staff_request(
    request_text: str,
    *,
    slack_repo_path: str | None = None,
    database_url: str | None = None,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> ChiefOfStaffResult:
    """Build a deterministic Chief of Staff routing recommendation without side effects."""

    del slack_repo_path  # Reserved for parity with SDK tools and future deterministic repo checks.
    text = str(request_text or "").strip()
    active_text = _latest_slack_followup_request(text) or text
    supplied_authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    request_plan = _chief_request_plan(text, manual_request_plan)

    def planned(result: ChiefOfStaffResult, *, action: str) -> ChiefOfStaffResult:
        return _with_manual_plan_audit(result, request_plan, action=action)

    if supplied_authority.canonical or supplied_authority.invalid:
        supplied_context_synthesis = _plan_operator_supplied_synthesis_request(
            active_text,
            manual_request_plan=request_plan,
        )
        if supplied_context_synthesis is not None:
            return planned(
                supplied_context_synthesis,
                action="allowed_semantic_operator_supplied_synthesis",
            )
        return planned(
            _plan_semantic_chief_recovery(request_plan),
            action="preserved_semantic_plan_during_recovery",
        )

    if _looks_like_slack_history_digest_request(text):
        return planned(
            _plan_slack_history_digest_request(text),
            action="allowed_slack_history_digest_shortcut",
        )
    if _finance_expense_receipt_provider_context(active_text):
        return planned(
            _plan_finance_tracker_request(active_text, live=False),
            action="allowed_finance_receipt_create_plan",
        )
    if _looks_like_finance_tracker_artifact_workflow_request(
        active_text
    ) and _manual_plan_allows_finance_tracker_shortcut(request_plan, active_text):
        return planned(
            _plan_finance_tracker_artifact_workflow_request(active_text),
            action="allowed_finance_artifact_workflow_shortcut",
        )
    if _looks_like_finance_tracker_request(
        active_text
    ) and _manual_plan_allows_finance_tracker_shortcut(request_plan, active_text):
        return planned(
            _plan_finance_tracker_request(active_text, live=False),
            action="allowed_finance_tracker_shortcut",
        )
    if (
        _chief_plan_requests_local_kni_documents(
            request_plan,
            request_text=active_text,
        )
        and (
            _looks_like_local_kni_document_lookup_request(active_text)
        )
    ):
        return planned(
            _plan_local_kni_document_lookup_request(active_text),
            action="allowed_local_kni_document_lookup",
        )
    if _looks_like_chief_memory_capture_request(text):
        return planned(
            _plan_chief_memory_capture(text, database_url=database_url),
            action="allowed_memory_capture_plan",
        )
    if _looks_like_chief_memory_review_request(text):
        return planned(
            _plan_chief_memory_review(text, database_url=database_url),
            action="allowed_memory_review_plan",
        )
    if _looks_like_reference_capture_request(text):
        return planned(
            _plan_reference_capture(text, database_url=database_url),
            action="allowed_reference_capture_plan",
        )
    if _looks_like_google_sheets_management_request(text):
        return planned(
            _plan_google_sheets_management_request(text),
            action="allowed_google_sheets_management_plan",
        )
    if _looks_like_google_drive_management_request(text):
        return planned(
            _plan_google_drive_management_request(text),
            action="allowed_google_drive_management_plan",
        )
    if _looks_like_artifact_write_request(text):
        return planned(
            _plan_business_artifact_write_request(text),
            action="allowed_business_artifact_write_plan",
        )
    supplied_context_synthesis = _plan_operator_supplied_synthesis_request(
        active_text,
        manual_request_plan=request_plan,
    )
    if supplied_context_synthesis is not None:
        return planned(
            supplied_context_synthesis,
            action="allowed_operator_supplied_synthesis",
        )
    if _looks_like_web_search_brief_request(active_text):
        return planned(
            _plan_web_search_brief_request(active_text),
            action="allowed_web_search_brief_plan",
        )
    natural_intent = _plan_natural_language_operating_intent(text, database_url=database_url)
    if natural_intent is not None:
        return planned(natural_intent, action="allowed_natural_language_operating_intent")
    if _looks_like_automation_inventory_request(text):
        report = build_automation_inventory_report(database_url=database_url)
        write_requests = _write_requests_from_text(text)
        return planned(
            ChiefOfStaffResult(
                mode="deterministic",
                intent=text,
                summary=report.summary,
                time_window=_extract_time_window(text),
                target_channels=_extract_target_channels(
                    text, _extract_target_channel(text, "ai-agents-workflow")
                ),
                operating_capabilities=_capabilities_for_request(text),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="slack-runtime-review",
                    command_text="@KNI chief of staff audit automations",
                    target_channel=_extract_target_channel(text, "ai-agents-workflow"),
                    rationale=(
                        "Chief of Staff can audit automation state and coordinate internal "
                        "review writes through typed tools."
                    ),
                    requires_live_connector=any(
                        request.live_required for request in write_requests
                    ),
                    requires_human_approval_before_post=True,
                ),
                recommended_actions=[
                    *report.recommended_actions,
                    *[
                        f"Prepare {request.destination.value} internal write request."
                        for request in write_requests
                    ],
                ],
                blocked_side_effects=BLOCKED_SIDE_EFFECTS,
                approval_required=True,
                human_review_required=True,
                send_enabled=False,
                slack_post_allowed=False,
                sources=_docs_for_topic(text),
                context_sources_considered=[
                    "business_workflow_state",
                    "operator_and_agent_policy",
                    "keystone_slack_runtime_repo",
                    "keystone_local_context",
                ],
                repo_context_used=_repo_context_for_capability("slack-runtime-review"),
                automation_report=report,
                write_requests=write_requests,
                audit_notes=[
                    "Deterministic Chief of Staff automation audit used.",
                    (
                        "Internal write requests are represented as typed plans; "
                        "live providers remain gated."
                    ),
                    "SQLite and WorkItems remain canonical.",
                ],
            ),
            action="allowed_automation_inventory_plan",
        )
    capability = _capability_for_topic(text)
    workflow_type = str(capability.get("workflow_type") or "clarification")
    target_channel = _extract_target_channel(text, str(capability.get("target_channel") or ""))
    target_channels = _extract_target_channels(text, target_channel)
    command = str(capability.get("command_text") or "/kni help")
    lowered_text = text.lower()
    if ("across" in lowered_text or "cross-channel" in lowered_text) and len(target_channels) > 1:
        workflow_type = "slack-cross-channel-review"
        command = "@KNI chief of staff summarize approved cross-channel context"
    elif (
        any(term in lowered_text for term in ("article", "articles", "link", "links"))
        and target_channels
    ):
        workflow_type = "slack-article-review"
        command = "@KNI chief of staff review channel links"
    elif any(
        term in lowered_text
        for term in ("follow-up", "follow up", "stale", "unresolved", "open loop")
    ):
        workflow_type = "slack-follow-up-review"
        command = "@KNI chief of staff find unresolved follow-ups"
    if (
        workflow_type == "calendar-read"
        and target_channel == "calendar"
        and "meeting" in text.lower()
    ):
        command = "/kni calendar today"
    if workflow_type in {"gmail-summary", "gmail-triage"} and "onboard" in text.lower():
        command = "/kni gmail summarize onboarding"
    summary = (
        "Explain Chief of Staff scope for KNI Slack operations."
        if workflow_type == "slack-runtime-review"
        else "Recommend a read-only Slack workflow and hold all posting for human approval."
        if workflow_type != "clarification"
        else "Need clarification before selecting a Slack operations workflow."
    )
    route = ChiefOfStaffRouteRecommendation(
        workflow_type=workflow_type,  # type: ignore[arg-type]
        command_text=command,
        target_channel=target_channel,
        rationale=str(capability.get("side_effect_policy") or "").strip()
        or "Chief of Staff v1 recommends routes only.",
        requires_live_connector=bool(capability.get("requires_live_connector")),
        requires_human_approval_before_post=True,
    )
    return planned(
        ChiefOfStaffResult(
            mode="deterministic",
            intent=text,
            summary=summary,
            time_window=_extract_time_window(text),
            target_channels=target_channels,
            operating_capabilities=_capabilities_for_request(text),
            recommended_route=route,
            recommended_actions=_action_lines(capability, target_channel or "selected channel"),
            blocked_side_effects=BLOCKED_SIDE_EFFECTS,
            approval_required=True,
            human_review_required=True,
            send_enabled=False,
            slack_post_allowed=False,
            sources=_docs_for_topic(text),
            context_sources_considered=[
                "operator_and_agent_policy",
                "keystone_slack_runtime_repo",
                "official_developer_docs",
                "keystone_local_context",
                "selected_gmail_context"
                if "mail" in text.lower() or "email" in text.lower()
                else "",
                "selected_calendar_context"
                if "calendar" in text.lower() or "meeting" in text.lower()
                else "",
                "github_and_local_repos"
                if "github" in text.lower() or "repo" in text.lower()
                else "",
            ],
            repo_context_used=_repo_context_for_capability(workflow_type),
            audit_notes=[
                "Deterministic Chief of Staff fallback used.",
                "KNI Slack repo is treated as read-only operational context.",
                "No Slack, Gmail, Calendar, CRM, or repo write was attempted.",
            ],
        ),
        action="allowed_general_route_plan",
    )


def _plan_semantic_chief_recovery(plan: ManualRequestPlan) -> ChiefOfStaffResult:
    """Preserve one validated semantic plan when live synthesis cannot be used."""

    workflow_type = (
        "gmail-triage"
        if plan.target_agent == "gmail_triage"
        else "research-direction-review"
        if plan.target_agent in {"business_research_analyst", "opportunity_scout"}
        else "business-agents-route"
    )
    objective = plan.objective or "Complete the interpreted operator request."
    return ChiefOfStaffResult(
        mode="llm_unavailable",
        intent=objective,
        summary=(
            "The live Chief response could not be validated. The preserved task is: "
            f"{objective} No provider action or completion is being claimed."
        ),
        operating_capabilities=[],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type=workflow_type,
            command_text="",
            target_channel="current-thread",
            rationale=(
                "Recovery preserved the LLM planner's owner and intent instead of "
                "reclassifying the request from keywords."
            ),
            requires_live_connector=False,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            f"Retry the preserved {plan.target_agent} task without changing its scope."
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[],
        context_sources_considered=[],
        audit_notes=[
            "Semantic-plan recovery used; no legacy phrase route was evaluated.",
            "No Slack, Gmail, Calendar, CRM, or provider write was attempted.",
        ],
    )


def _looks_like_automation_inventory_request(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "automation",
            "automations",
            "audit current",
            "current runs",
        )
    )


def _looks_like_google_drive_management_request(text: str) -> bool:
    lowered = text.lower()
    has_drive_surface = any(
        marker in lowered for marker in ("google drive", "gdrive", "drive folder", "knio", "kniops")
    )
    has_management_intent = any(
        marker in lowered
        for marker in (
            "folder",
            "subfolder",
            "folders",
            "list",
            "what is in",
            "what's in",
            "rename",
            "manage",
            "move",
        )
    )
    return has_drive_surface and has_management_intent


def _looks_like_google_sheets_management_request(text: str) -> bool:
    lowered = text.lower()
    has_sheet_surface = any(
        marker in lowered
        for marker in (
            "google sheet",
            "google sheets",
            "spreadsheet",
            "sheet",
            "structured data",
        )
    )
    has_management_intent = any(
        marker in lowered
        for marker in (
            "create",
            "read",
            "list",
            "append",
            "add row",
            "update",
            "modify",
            "delete",
            "trash",
            "tab",
            "row",
            "table",
        )
    )
    return has_sheet_surface and has_management_intent


def _plan_google_sheets_management_request(text: str) -> ChiefOfStaffResult:
    lowered = text.lower()
    title_match = re.search(
        r"\b(?:sheet|spreadsheet)\s+named\s+([\w][\w ._-]{0,120}?)(?=\s+in\s+|[,.;]|$)",
        text,
        flags=re.I,
    )
    requested_title = (
        " ".join(title_match.group(1).split())
        if title_match is not None
        else "KNIOps Structured Data"
    )
    requested_operations = [
        operation
        for operation, markers in (
            ("create", ("create",)),
            ("append_row", ("append", "add row", "add a marked")),
            ("read_back", ("read", "verify", "confirm")),
            ("update_row", ("update", "modify")),
            ("delete_row", ("delete", "remove row")),
            ("trash_sheet", ("trash", "move the same test sheet to trash")),
        )
        if any(marker in lowered for marker in markers)
    ]
    write_requests: list[ChiefOfStaffWriteRequest] = []
    if any(
        marker in lowered
        for marker in ("create", "append", "add row", "update", "modify", "delete", "trash")
    ):
        write_requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.GOOGLE_SHEET,
                title=requested_title,
                summary=(
                    "Manage structured Google Sheets data only within the configured "
                    "KNIOps Google Drive boundary."
                ),
                approval_required=True,
                live_required=True,
                metadata={
                    "owner_agent": "google_workspace_context_agent",
                    "scope": "KNIOps",
                    "default_workbook": "KNIOps Structured Data",
                    "requested_title": requested_title,
                    "requested_operations": requested_operations,
                    "requires_provider_readback": True,
                    "requires_cleanup_verification": "trash_sheet" in requested_operations,
                    "delete_policy": "trash spreadsheet files or remove explicit rows/tabs only",
                },
            )
        )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=(
            f"Prepare an approval-gated Google Workspace lifecycle for '{requested_title}' "
            "under KNIOps, retaining the exact Sheet identity through provider read-back "
            "and requested cleanup."
        ),
        time_window=_extract_time_window(text),
        target_channels=_extract_target_channels(text, _extract_target_channel(text, "docs")),
        operating_capabilities=[
            *_capabilities_for_request(text),
            "google_sheets_structured_data",
        ],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="google-drive-management",
            command_text="@KNI chief of staff manage KNIOps Google Sheets structured data",
            target_channel=_extract_target_channel(text, "docs"),
            rationale=(
                "Google Sheets operations are internal structured-data writes bounded "
                "to KNIOps and gated by explicit approval."
            ),
            requires_live_connector=True,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            "Use google_sheet_list or google_sheet_read_table for read-only inspection.",
            "Use google_sheet_create, append_rows, update_row, and tab tools for approved changes.",
            "Trash spreadsheet files only when explicitly requested; do not permanently delete.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        sources=[],
        context_sources_considered=[
            "operator_and_agent_policy",
            "keystone_slack_runtime_repo",
            "google_workspace_knioops",
        ],
        repo_context_used=_repo_context_for_capability("google-drive-management"),
        write_requests=write_requests,
        audit_notes=[
            "Deterministic Chief of Staff Google Sheets management routing used.",
            (
                "Google Sheets are structured internal records; Google Docs remain "
                "narrative artifacts."
            ),
            (
                "Sheet contents do not authorize Gmail, Slack, outreach, CRM, or "
                "calendar side effects."
            ),
        ],
    )


def _plan_google_drive_management_request(text: str) -> ChiefOfStaffResult:
    lowered = text.lower()
    write_requests: list[ChiefOfStaffWriteRequest] = []
    if any(marker in lowered for marker in ("create", "rename", "modify", "move")):
        write_requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.GOOGLE_DRIVE_FOLDER,
                title="KNIOps Folder Management",
                summary=(
                    "Manage folders only within the configured KNIOps Google Drive folder tree."
                ),
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason=(
                    "Live Google Drive folder writes require an approval reference and "
                    "GOOGLE_WORKSPACE_WRITES_ENABLED=true."
                ),
                metadata=json.dumps(
                    {
                        "boundary": "KNIOps",
                        "allowed_operations": ["list", "create_subfolder", "rename_subfolder"],
                        "disallowed_operations": ["delete", "share_outside_boundary"],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
        )
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=(
            "Use scoped Google Drive tools for the KNIOps folder tree: list folder "
            "contents, create subfolders with approval, rename subfolders with approval, "
            "and read or write Google Docs only inside that tree."
        ),
        time_window=_extract_time_window(text),
        target_channels=_extract_target_channels(text),
        operating_capabilities=[
            "document_review",
            "artifact_write_planning",
            "google_drive_folder_management",
        ],
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="google-drive-management",
            command_text="@KNI chief of staff manage the KNIOps Drive folder",
            target_channel=_extract_target_channel(text, "docs"),
            rationale=(
                "Google Drive management must stay inside KNIOps and use typed Drive/Docs "
                "tools rather than broad Drive browsing."
            ),
            requires_live_connector=True,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=[
            (
                "List KNIOps or a named subfolder with `google_drive_list_folder` "
                "when live OAuth is available."
            ),
            (
                "Create or rename subfolders only with an approval reference and "
                "`GOOGLE_WORKSPACE_WRITES_ENABLED=true`."
            ),
            "Create, read, or update Google Docs only inside KNIOps or its subfolders.",
            "Do not delete, share, or modify Drive items outside KNIOps.",
        ],
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        slack_post_policy="requires_human_review",
        sources=_docs_for_topic(text),
        context_sources_considered=[
            "operator_and_agent_policy",
            "keystone_local_context",
            "keystone_slack_runtime_repo",
            "google_workspace_oauth",
        ],
        repo_context_used=_repo_context_for_capability("business-agents-route"),
        write_requests=write_requests,
        audit_notes=[
            "Chief of Staff planned a scoped Google Drive folder management path.",
            "KNIOps is the only permitted Drive boundary for this capability.",
            "No live Google Drive write was attempted by the deterministic planner.",
        ],
    )


def _write_requests_from_text(text: str) -> list[ChiefOfStaffWriteRequest]:
    lowered = text.lower()
    requests: list[ChiefOfStaffWriteRequest] = []
    if "google doc" in lowered or "google docs" in lowered or "doc" in lowered:
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.GOOGLE_DOC,
                title="Internal Google Doc",
                summary="Create an internal Google Doc for the requested analysis or report.",
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason=(
                    "Live Google Docs writes require an approval reference and "
                    "GOOGLE_WORKSPACE_WRITES_ENABLED=true."
                ),
            )
        )
    if "folder" in lowered and any(
        marker in lowered
        for marker in ("google drive", "gdrive", "drive", "google doc", "google docs")
    ):
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.GOOGLE_DRIVE_FOLDER,
                title="KNIOps Folder Management",
                summary=(
                    "Create or use a scoped KNIOps Drive subfolder for the requested "
                    "internal artifact."
                ),
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason=(
                    "Live Google Drive folder writes require an approval reference and "
                    "GOOGLE_WORKSPACE_WRITES_ENABLED=true."
                ),
                metadata=json.dumps(
                    {
                        "boundary": "KNIOps",
                        "requested_surface": "google_drive_folder",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )
        )
    if _requests_airtable_write_surface(lowered):
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.AIRTABLE,
                title="Automation Inventory Airtable Mirror",
                summary="Sync automation findings to an Airtable-shaped review table.",
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason=(
                    "Live Airtable writes require an approval reference, "
                    "AIRTABLE_ALLOW_WRITES=true, and AIRTABLE_WRITE_DRY_RUN=false."
                ),
            )
        )
    if "slack" in lowered:
        requests.append(
            ChiefOfStaffWriteRequest(
                destination=AutomationWriteDestination.SLACK,
                title="Automation Inventory Slack Summary",
                summary="Post a short internal Slack review summary.",
                approval_required=True,
                live_required=True,
                allowed=False,
                status="planned",
                blocked_reason="Public Slack posts remain approval-gated.",
            )
        )
    return requests


def _requests_airtable_write_surface(lowered: str) -> bool:
    if "airtable" not in lowered:
        return False
    return bool(
        re.search(
            r"\b(save|add|write|publish|mirror|sync|create|update|organize)\b.{0,80}"
            r"\b(?:to|into|in)?\s*airtable\b",
            lowered,
        )
        or re.search(
            r"\bairtable\b.{0,80}\b(record|row|mirror|write|publish|sync|update)\b",
            lowered,
        )
    )


def _looks_like_artifact_write_request(text: str) -> bool:
    lowered = text.lower()
    has_google_docs_surface = any(marker in lowered for marker in ("google doc", "google docs"))
    has_airtable_surface = "airtable" in lowered
    has_surface = has_google_docs_surface or has_airtable_surface or "artifact" in lowered
    has_text_artifact = has_google_docs_surface and any(
        marker in lowered
        for marker in (
            "note",
            "notes",
            "brief",
            "summary",
            "writeup",
            "write-up",
            "memo",
            "decision log",
            "meeting",
            "research",
            "artifact write test",
            "draft artifact",
            "text",
        )
    )
    has_business_artifact = any(
        marker in lowered
        for marker in (
            "company info",
            "company profile",
            "company note",
            "company notes",
            "company artifact",
            "contact info",
            "contact details",
            "contacts",
            "lead",
            "account info",
        )
    )
    return has_surface and (has_business_artifact or has_text_artifact)


def _artifact_type_from_text(text: str) -> str:
    lowered = text.lower()
    if "contact" in lowered:
        return "contact_candidates"
    if "company" in lowered or "account" in lowered:
        return "company_profile"
    if "decision" in lowered:
        return "decision_log"
    if "meeting" in lowered:
        return "meeting_brief"
    if "research" in lowered:
        return "research_brief"
    if "memo" in lowered:
        return "memo"
    if "note" in lowered:
        return "note"
    if "summary" in lowered:
        return "summary"
    return "business_artifact"


def _plan_business_artifact_write_request(text: str) -> ChiefOfStaffResult:
    artifact_type = _artifact_type_from_text(text)
    channels = _extract_target_channels(text, "ai-agents-workflow")
    write_requests = []
    for request in _write_requests_from_text(text):
        write_requests.append(
            request.model_copy(
                update={
                    "title": (
                        "Company Profile Artifact"
                        if artifact_type == "company_profile"
                        else "Contact Candidate Artifact"
                        if artifact_type == "contact_candidates"
                        else "Text Artifact"
                        if request.destination == AutomationWriteDestination.GOOGLE_DOC
                        else "Business Artifact"
                    ),
                    "summary": (
                        "Prepare a source-backed structured artifact for review and optional "
                        f"{request.destination.value} publishing."
                        if request.destination == AutomationWriteDestination.AIRTABLE
                        else "Prepare a source-backed internal text artifact for Google Docs."
                    ),
                    "metadata": json.dumps(
                        {
                            "artifact_type": artifact_type,
                            "canonical_state": "sqlite",
                            "requires_source_backing": True,
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    ),
                }
            )
        )
    destinations = {request.destination for request in write_requests}
    google_doc_text_only = destinations == {
        AutomationWriteDestination.GOOGLE_DOC
    } and artifact_type not in {"company_profile", "contact_candidates"}
    return ChiefOfStaffResult(
        mode="deterministic",
        intent=text,
        summary=(
            "Plan an internal Google Docs text artifact in the KNIOps Drive folder. "
            "Use the supplied title/body when present, keep the artifact internal, "
            "and only create or update live Docs with an approval reference."
            if google_doc_text_only
            else (
                "Plan a source-backed internal business artifact before any Airtable or "
                "Google Docs write. Use Business Research Analyst or Opportunity Scout "
                "for missing company/contact facts, then publish only reviewed structured fields."
            )
        ),
        time_window=_extract_time_window(text),
        target_channels=channels,
        operating_capabilities=_capabilities_for_request(text),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="artifact-write-plan",
            command_text=(
                "@KNI chief of staff create or update an internal Google Doc text artifact"
                if google_doc_text_only
                else "@KNI business research analyst <company/contact artifact request>"
            ),
            target_channel=channels[0] if channels else "ai-agents-workflow",
            rationale=(
                "Google Docs text artifacts can be created or updated in the KNIOps "
                "Drive folder when the title/body and approval boundary are clear."
                if google_doc_text_only
                else (
                    "Company and contact artifacts need source-backed structured data before "
                    "mirroring to Airtable or Google Docs."
                )
            ),
            requires_live_connector=True,
            requires_human_approval_before_post=True,
        ),
        recommended_actions=(
            [
                "Resolve the Google Doc title and text body.",
                (
                    "Create new Docs in the KNIOps Drive folder, or update an existing "
                    "KNIOps doc by id."
                ),
                "Require an approval reference before any live Google Docs write.",
            ]
            if google_doc_text_only
            else [
                "Resolve the company/contact target and collect source-backed facts.",
                "Create a structured artifact in SQLite as canonical state.",
                (
                    "Mirror reviewed fields to Airtable or Google Docs only through typed "
                    "publisher tools."
                ),
            ]
        ),
        blocked_side_effects=BLOCKED_SIDE_EFFECTS,
        approval_required=True,
        human_review_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        slack_post_policy="requires_human_review",
        sources=_docs_for_topic(text),
        context_sources_considered=[
            "operator_and_agent_policy",
            "business_workflow_state",
            "keystone_local_context",
            "keystone_slack_runtime_repo",
        ],
        repo_context_used=_repo_context_for_capability("business-agents-route"),
        write_requests=write_requests,
        audit_notes=[
            "Chief of Staff planned an internal artifact write path.",
            "Airtable and Google Docs are review surfaces; SQLite remains canonical.",
            "No live provider write was attempted by the deterministic planner.",
        ],
    )


def build_chief_of_staff_agent(
    model: str | None = None,
    *,
    quality_budget: AgentQualityBudget | None = None,
    quality_mode: QualityMode | str | None = None,
    include_specialist_tools: bool | None = None,
    specialist_tool_mode: SpecialistToolMode = "read_plan",
    request_text: str = "",
    manual_request_plan: ManualRequestPlan | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    attach_tools: bool = True,
) -> Agent:
    """Build the KNI Chief of Staff SDK agent."""

    budget = quality_budget or chief_of_staff_quality_budget(
        quality_mode,
        request_text=request_text,
        live_sdk=False,
        manual_request_plan=manual_request_plan,
    )
    direct_supplied_synthesis = _chief_direct_supplied_synthesis(
        request_text,
        manual_request_plan,
    )
    instructions = (
        compose_direct_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "chief_of_staff_supplied_synthesis_compact.md",
        )
        if direct_supplied_synthesis
        else compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "tools.md",
            "chief_of_staff.md",
            skill_files=select_agent_skill_names(
                "chief_of_staff",
                request_text=request_text,
                context_flags=context_flags,
                include_all=include_all_skills,
            ),
        )
    )
    resolved_include_specialist_tools = (
        include_specialist_tools
        if include_specialist_tools is not None
        else _env_flag_enabled(CHIEF_OF_STAFF_SPECIALIST_TOOLS_ENV)
    )
    resolved_specialist_tool_mode = _normalize_specialist_tool_mode(specialist_tool_mode)
    selected_specialist_routes = _chief_specialist_routes_from_plan(
        manual_request_plan
    )
    specialist_tools = (
        build_specialist_agent_tools(
            manager_agent_name="chief_of_staff",
            mode=resolved_specialist_tool_mode,
            raw_operator_request=request_text,
            manual_request_plan=manual_request_plan,
            include_routes=selected_specialist_routes,
        )
        if resolved_include_specialist_tools
        and (selected_specialist_routes is None or selected_specialist_routes)
        else []
    )
    resolved_attach_tools = bool(
        attach_tools
        and not direct_supplied_synthesis
        and (
            budget.max_tool_calls != 0
            or _chief_semantic_tools_required(manual_request_plan)
        )
    )
    return build_sdk_agent(
        name="chief_of_staff",
        instructions=instructions,
        output_type=ChiefOfStaffResult,
        tools=(
            _chief_of_staff_tools(
                request_text,
                manual_request_plan=manual_request_plan,
                specialist_tools=specialist_tools,
            )
            if resolved_attach_tools
            else []
        ),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name="chief_of_staff",
        model_settings=build_model_settings(
            reasoning_effort=budget.reasoning_effort or CHIEF_OF_STAFF_REASONING_EFFORT,
            verbosity=budget.verbosity or CHIEF_OF_STAFF_VERBOSITY,
            max_tokens=budget.max_tokens or CHIEF_OF_STAFF_MAX_TOKENS,
        ),
        handoff_description=(
            "Use to plan KNI Slack operations routing, scoped internal Slack communication, "
            "and bounded internal review writes across automation, calendar, Gmail, "
            "business-agent, and Slack workflows."
        ),
    )


def run_chief_of_staff_sdk(
    typed_input: str | Mapping[str, Any],
    *,
    run_config: Any | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    quality_mode: QualityMode | str | None = None,
    quality_budget: AgentQualityBudget | None = None,
    force_sdk_interpretation: bool = False,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_specialist_tools: bool | None = None,
    specialist_tool_mode: SpecialistToolMode = "read_plan",
    attach_tools: bool = True,
) -> TypedAgentRunResult[ChiefOfStaffResult]:
    """Run Chief of Staff through the shared typed SDK harness."""

    if isinstance(typed_input, Mapping):
        request_text = str(typed_input.get("request") or "")
        if manual_request_plan is None and "manual_request_plan" in typed_input:
            manual_request_plan = typed_input.get("manual_request_plan")
        if include_specialist_tools is None and "include_specialist_tools" in typed_input:
            include_specialist_tools = bool(typed_input.get("include_specialist_tools"))
        specialist_tool_mode = _normalize_specialist_tool_mode(
            typed_input.get("specialist_tool_mode") or specialist_tool_mode
        )
    else:
        request_text = str(typed_input or "")
    request_plan = _chief_request_plan(request_text, manual_request_plan)
    typed_input_for_run = _chief_of_staff_sdk_input_for_request(
        typed_input,
        raw_request_text=request_text,
        manual_request_plan=request_plan,
    )
    active_request_text = _latest_slack_followup_request(request_text) or request_text
    if include_specialist_tools is None:
        include_specialist_tools = chief_of_staff_should_use_specialist_tools(
            active_request_text,
            request_plan,
        )
    if (
        not force_sdk_interpretation
        and not live
        and _looks_like_slack_history_digest_request(request_text)
    ):
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=_with_manual_plan_audit(
                _plan_slack_history_digest_request(request_text),
                request_plan,
                action="allowed_slack_history_digest_shortcut",
            ),
            raw_result={"deterministic": "slack_history_digest"},
            live=live,
        )
    finance_artifact_workflow = bool(
        _looks_like_finance_tracker_artifact_workflow_request(active_request_text)
        and _manual_plan_allows_finance_tracker_shortcut(
            request_plan,
            active_request_text,
        )
    )
    if (
        _finance_expense_receipt_provider_context(active_request_text)
        and _manual_plan_authorizes_finance_expense_receipt(request_plan)
        and not live
        and not force_sdk_interpretation
    ):
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=_with_manual_plan_audit(
                _plan_finance_tracker_request(active_request_text, live=False),
                request_plan,
                action="allowed_finance_receipt_create_plan",
            ),
            raw_result={"deterministic": "finance_tax_tracker_receipt_create_plan"},
            live=live,
        )
    if finance_artifact_workflow and not live:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=_with_manual_plan_audit(
                _plan_finance_tracker_artifact_workflow_request(active_request_text),
                request_plan,
                action="allowed_finance_artifact_workflow_shortcut",
            ),
            raw_result={"deterministic": "finance_tax_tracker_artifact_plan"},
            live=live,
        )
    if (
        _looks_like_finance_tracker_request(active_request_text)
        and not finance_artifact_workflow
        and not force_sdk_interpretation
        and not (live and _finance_expense_receipt_provider_context(active_request_text))
        and not (
            live
            and _looks_like_finance_tracker_mutation_request(active_request_text)
            and not _looks_like_expense_total_sync_request(
                _normalized_text(active_request_text)
            )
        )
        and _manual_plan_allows_finance_tracker_shortcut(request_plan, active_request_text)
    ):
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=_with_manual_plan_audit(
                _plan_finance_tracker_request(active_request_text, live=live),
                request_plan,
                action="allowed_finance_tracker_shortcut",
            ),
            raw_result={"deterministic": "finance_tax_tracker"},
            live=live,
        )
    if live:
        live_preflight_blocker = _finance_expense_receipt_live_preflight_blocker(
            active_request_text,
            manual_request_plan=request_plan,
        )
        if live_preflight_blocker is not None:
            return TypedAgentRunResult(
                agent_name="chief_of_staff",
                output=_with_manual_plan_audit(
                    live_preflight_blocker,
                    request_plan,
                    action="blocked_receipt_attachment_upload_gate",
                ),
                raw_result={
                    "blocked": "airtable_receipt_attachment_upload_gate",
                    "missing_env": "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS",
                },
                live=False,
            )

    budget = quality_budget or chief_of_staff_quality_budget(
        quality_mode,
        request_text=request_text,
        live_sdk=live,
        manual_request_plan=request_plan,
    )
    result = run_typed_sdk_agent(
        agent=build_chief_of_staff_agent(
            model=model,
            quality_budget=budget,
            include_specialist_tools=include_specialist_tools,
            specialist_tool_mode=specialist_tool_mode,
            request_text=request_text,
            manual_request_plan=request_plan,
            context_flags=context_flags,
            attach_tools=attach_tools,
        ),
        typed_input=typed_input_for_run,
        output_type=ChiefOfStaffResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=budget.max_turns,
        trace_metadata={
            "quality_mode": budget.mode.value,
            "quality_max_turns": budget.max_turns,
            "quality_reasoning_effort": budget.reasoning_effort,
            "quality_output_budget": budget.max_tokens,
        },
    )
    note = (
        f"Chief of Staff quality budget used: {budget.mode.value}; "
        f"max_turns={budget.max_turns}; reasoning_effort={budget.reasoning_effort}."
    )
    output = result.output
    if _chief_direct_supplied_synthesis(active_request_text, request_plan):
        output = output.model_copy(
            update={
                "recommended_route": ChiefOfStaffRouteRecommendation(
                    workflow_type="project-context-review",
                    command_text="",
                    target_channel="current-thread",
                    rationale=(
                        "The operator supplied sufficient context for a direct "
                        "provider-free answer."
                    ),
                    requires_live_connector=False,
                    requires_human_approval_before_post=True,
                ),
                "durable_handoff": None,
                "context_handoffs": [],
            }
        )
    audit_notes = list(output.audit_notes)
    if note not in audit_notes:
        audit_notes.append(note)
    return replace(result, output=output.model_copy(update={"audit_notes": audit_notes}))


def render_chief_of_staff_result(result: ChiefOfStaffResult) -> str:
    """Render a compact operator-readable Chief of Staff result."""

    route = result.recommended_route
    lines = [
        "Agent: KNI Chief of Staff Agent",
        f"Workflow: {route.workflow_type}",
        f"Command: {route.command_text or '(clarify)'}",
        (
            f"Target channel: #{route.target_channel}"
            if route.target_channel
            else "Target channel: clarify"
        ),
        f"Send enabled: {result.send_enabled}",
        f"Slack post allowed: {result.slack_post_allowed}",
        f"Summary: {result.summary}",
    ]
    if result.recommended_actions:
        lines.append("Actions:")
        lines.extend(f"- {action}" for action in result.recommended_actions)
    return "\n".join(lines)
