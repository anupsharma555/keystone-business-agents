"""Request-scoped tool attachment for bounded SDK agent runs.

The registry and each ``AgentToolPolicy`` describe the complete capability
ceiling for an agent.  This module compiles the smaller effective toolbox for
one request.  A canonical semantic plan may enlarge that toolbox; raw request
wording and compatibility/heuristic plans may not.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from keystone_agents.agent_tool_policy import (
    AIRTABLE_READ_ALLOWED_TOOLS,
    AIRTABLE_TEST_LIFECYCLE_TOOLS,
    AIRTABLE_WRITE_ALLOWED_TOOLS,
    BROWSER_DIAGNOSTIC_ALLOWED_TOOLS,
    CALENDAR_READ_TOOL_NAMES,
    CALENDAR_WRITE_TOOL_NAMES,
    DEEP_RETRIEVAL_TOOL_NAMES,
    DIAGNOSTIC_TOOL_NAMES,
    GOOGLE_WORKSPACE_READ_TOOLS,
    GOOGLE_WORKSPACE_WRITE_TOOLS,
    PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS,
    WEB_SEARCH_TOOL_NAMES,
    ToolTier,
    normalize_tool_tier,
    tool_name_for_policy,
    tool_tier_for_name,
)
from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.planning.compatibility import (
    looks_like_explicit_public_web_research,
    request_forbids_live_research,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.specialist_tool_names import (
    SPECIALIST_AGENT_TOOL_SUFFIX,
    is_specialist_agent_tool_name,
)
from keystone_agents.tools.zotero_context_tools import (
    ZOTERO_IMPORT_TOOL_NAMES,
    ZOTERO_READ_CONTEXT_TOOL_NAMES,
    ZOTERO_TEST_LIBRARY_TOOL_NAMES,
    ZOTERO_TEST_NOTE_TOOL_NAMES,
)


class ToolScopeMode(StrEnum):
    """Supported effective-toolbox modes."""

    AUTO = "auto"
    REQUEST_SCOPED = "request_scoped"
    FULL = "full"


class ToolCapabilityFamily(StrEnum):
    """Request-level reasons for attaching a related set of tools."""

    CORE_CONTEXT = "core_context"
    PUBLIC_WEB_SEARCH = "public_web_search"
    DEEP_RETRIEVAL = "deep_retrieval"
    BROWSER_DIAGNOSTICS = "browser_diagnostics"
    PROVIDER_READ = "provider_read"
    DRAFT_PREPARATION = "draft_preparation"
    DURABLE_INTERNAL_STATE = "durable_internal_state"
    PROVIDER_WRITE = "provider_write"
    FULL_INSPECTION = "full_inspection"


class RequestToolScopeReceipt(BaseModel):
    """Audit evidence for the exact toolbox attached to one model request."""

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.request_tool_scope.v1"
    agent_name: str
    requested_mode: ToolScopeMode
    effective_mode: ToolScopeMode
    source: str
    max_tool_tier: str
    capability_families: tuple[ToolCapabilityFamily, ...] = ()
    candidate_tool_count: int = Field(ge=0)
    candidate_tool_names: tuple[str, ...] | None = None
    selected_tool_names: tuple[str, ...] = ()
    selected_tool_count: int = Field(ge=0)
    omitted_tool_count: int = Field(ge=0)
    omitted_tool_names: tuple[str, ...] | None = None
    omission_reasons: tuple[str, ...] | None = None
    reduction_ratio: float = Field(ge=0.0, le=1.0)
    selection_fingerprint: str
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_counts(self) -> RequestToolScopeReceipt:
        if self.selected_tool_count != len(self.selected_tool_names):
            raise ValueError("selected_tool_count must equal selected_tool_names length")
        if self.candidate_tool_count != self.selected_tool_count + self.omitted_tool_count:
            raise ValueError("candidate count must equal selected plus omitted tool counts")
        if len(set(self.selected_tool_names)) != len(self.selected_tool_names):
            raise ValueError("selected tool names must be unique")

        diagnostic_fields = (
            self.candidate_tool_names,
            self.omitted_tool_names,
            self.omission_reasons,
        )
        if all(value is None for value in diagnostic_fields):
            # Receipts emitted before these diagnostic fields were introduced are
            # still valid v1 evidence. Keep their known counts intact and serialize
            # the unknown identities explicitly as null rather than inventing them.
            return self
        if any(value is None for value in diagnostic_fields):
            raise ValueError(
                "candidate_tool_names, omitted_tool_names, and omission_reasons "
                "must be supplied together"
            )

        candidate_tool_names = self.candidate_tool_names
        omitted_tool_names = self.omitted_tool_names
        assert candidate_tool_names is not None
        assert omitted_tool_names is not None
        if self.candidate_tool_count != len(candidate_tool_names):
            raise ValueError("candidate_tool_count must equal candidate_tool_names length")
        if self.omitted_tool_count != len(omitted_tool_names):
            raise ValueError("omitted_tool_count must equal omitted_tool_names length")
        for label, values in (
            ("candidate", candidate_tool_names),
            ("omitted", omitted_tool_names),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} tool names must be unique")
        if set(self.selected_tool_names).intersection(omitted_tool_names):
            raise ValueError("selected and omitted tool names must be disjoint")
        if set(candidate_tool_names) != {
            *self.selected_tool_names,
            *omitted_tool_names,
        }:
            raise ValueError("candidate tools must equal selected plus omitted tools")
        return self

    def receipt(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True)
class ScopedToolAttachment:
    """Concrete scoped SDK tools paired with their audit receipt."""

    tools: tuple[Any, ...]
    scope: RequestToolScopeReceipt


# These are small, agent-owned context surfaces rather than every tool in the
# broad ``core_read`` tier.  They remain side-effect-free and useful when a
# planner is unavailable or a compatibility request is replayed.
_CORE_CONTEXT_BY_AGENT: dict[str, frozenset[str]] = {
    "business_research_analyst": frozenset(
        {
            "load_contact_context",
            "load_crm_account_context",
            "load_approved_contact_context",
            "load_approved_crm_context",
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "retrieve_memory",
            "read_web_source_window",
            "read_work_item_source_evidence",
            "check_workflow_duplicate",
        }
    ),
    "opportunity_scout": frozenset(
        {
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "retrieve_memory",
            "read_web_source_window",
            "check_workflow_duplicate",
            "load_existing_opportunity_state",
        }
    ),
    "outreach_composer": frozenset(
        {
            "load_company_profile",
            "load_research_brief_profile",
            "load_opportunity_record",
            "load_contact_context",
            "load_crm_account_context",
            "load_style_profile",
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "list_outreach_templates",
            "load_outreach_template",
            "retrieve_outreach_example_guidance",
            "load_email_style_profile",
            "retrieve_memory",
            "retrieve_outreach_examples",
            "check_workflow_duplicate",
            "load_approved_contact_context",
            "load_approved_crm_context",
            "load_approved_outreach_examples",
            "check_unsupported_claims",
            "list_outreach_tracking_records",
        }
    ),
    "gmail_triage": frozenset(
        {
            "inspect_gmail_mailbox_schema",
            "query_gmail_message_summaries",
            "read_gmail_context",
            "get_gmail_message",
            "load_email_style_profile",
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "retrieve_memory",
            "list_outreach_tracking_records",
        }
    ),
    "orchestrator": frozenset(
        {
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "retrieve_memory",
            "route_request_placeholder",
            "load_orchestrator_workflow_state",
            "load_pending_approval_items",
            "inspect_work_item_execution_receipts",
        }
    ),
    "chief_of_staff": frozenset(
        {
            "list_chief_of_staff_context_sources",
            "list_slack_slash_commands",
            "summarize_slack_runtime_config",
            "search_slack_repo_context",
            "read_slack_repo_context_file",
            "lookup_slack_workflow_capability",
            "validate_slack_slash_command",
            "retrieve_chief_of_staff_memory",
            "list_local_context_sources",
            "search_local_context",
            "read_local_context_file",
            "list_automation_specs",
            "list_recent_automation_runs",
            "list_channel_automation_bindings",
            "summarize_automation_health",
            "list_pending_automation_approvals",
            "inspect_active_work_item_execution_summary",
            "inspect_active_work_items",
            "inspect_work_item_execution_receipts",
            "list_kni_document_folder",
            "list_kni_document_sources",
            "search_kni_documents",
            "read_kni_document_file",
            "read_linked_article",
            "read_google_calendar_window",
            "file_search",
        }
    ),
    "rss_context_agent": frozenset(
        {
            "retrieve_rss_announcement_history",
            "read_rss_announcement_evidence",
            "inspect_signal_lifecycle",
        }
    ),
    "preprints_context_agent": frozenset(
        {
            "retrieve_preprint_announcement_history",
            "read_preprint_announcement_evidence",
            "inspect_signal_lifecycle",
        }
    ),
}

# Operational state inspection is a common manager request, but it should not
# attach every unrelated repository, document, automation, and provider reader.
# Manager builders select one bounded surface for the request shape: a compact
# comparison view for inventory questions or exact receipt inspection when a
# WorkItem id is already known. The larger set here is only an allowlist; it
# does not cause every tool to be attached.
_WORK_ITEM_INSPECTION_BY_AGENT: dict[str, frozenset[str]] = {
    "orchestrator": frozenset(
        {
            "load_orchestrator_workflow_state",
            "inspect_work_item_execution_receipts",
            "load_pending_approval_items",
        }
    ),
    "chief_of_staff": frozenset(
        {
            "inspect_active_work_item_execution_summary",
            "inspect_active_work_items",
            "inspect_work_item_execution_receipts",
            "list_pending_automation_approvals",
        }
    ),
}

_DRAFT_PREPARATION_BY_AGENT: dict[str, frozenset[str]] = {
    "outreach_composer": frozenset(
        {
            "check_unsupported_claims",
            "build_approved_outreach_drafting_context",
            "compose_outreach_draft_llm_constrained",
            "build_call_prep_artifact",
            "build_follow_up_schedule_record",
        }
    ),
    "gmail_triage": frozenset(
        {
            "create_gmail_draft_reply",
            "create_gmail_draft_with_attachment",
            "create_approval_queue_item",
        }
    ),
}

# An exact, operator-selected public page is a different retrieval shape from
# broad research. It needs the bounded page extractor, not search, contact
# discovery, local context, or the rest of the deep-retrieval toolbox.
_SELECTED_SOURCE_READ_BY_AGENT: dict[str, frozenset[str]] = {
    "business_research_analyst": frozenset(
        {
            "extract_selected_urls_to_source_bundle",
            "read_web_source_window",
        }
    ),
}

_SUPPLIED_OPPORTUNITY_ANALYSIS_BY_AGENT: dict[str, frozenset[str]] = {
    "opportunity_scout": frozenset({"score_opportunity"}),
}

_SUPPLIED_OUTREACH_DRAFT_BY_AGENT: dict[str, frozenset[str]] = {
    "outreach_composer": frozenset(
        {
            "check_unsupported_claims",
            "build_approved_outreach_drafting_context",
            "compose_outreach_draft_llm_constrained",
        }
    ),
}

_DURABLE_STATE_BY_AGENT: dict[str, frozenset[str]] = {
    "business_research_analyst": frozenset(
        {"save_company_profile_memory", "save_retrieval_tool_performance_memory"}
    ),
    "opportunity_scout": frozenset(
        {"save_entity_memory", "save_opportunity_memory", "save_opportunity_placeholder"}
    ),
    "outreach_composer": frozenset(
        {
            "save_outreach_dedup_memory",
            "learn_email_style_profile",
            "save_initial_outreach_tracking_record",
            "create_approval_queue_item",
            "create_approval_request_placeholder",
        }
    ),
    "rss_context_agent": frozenset(
        {
            "prepare_signal_lifecycle_checkpoint",
            "advance_signal_lifecycle_checkpoint",
        }
    ),
    "preprints_context_agent": frozenset(
        {
            "prepare_signal_lifecycle_checkpoint",
            "advance_signal_lifecycle_checkpoint",
        }
    ),
}

_GMAIL_READ_TOOLS = frozenset(
    {
        "inspect_gmail_mailbox_schema",
        "query_gmail_message_summaries",
        "read_gmail_context",
        "get_gmail_message",
    }
)
_GMAIL_WRITE_TOOLS = frozenset(
    {
        "apply_gmail_labels",
        "modify_gmail_message_state",
        "create_gmail_draft_reply",
        "create_gmail_draft_with_attachment",
        "gmail_test_draft_lifecycle",
        "send_gmail_test_draft",
    }
)

_PROVIDER_READ_TOOLS: dict[str, frozenset[str]] = {
    "airtable": AIRTABLE_READ_ALLOWED_TOOLS,
    "google_workspace": GOOGLE_WORKSPACE_READ_TOOLS,
    "google_calendar": CALENDAR_READ_TOOL_NAMES,
    "gmail": _GMAIL_READ_TOOLS,
    "zotero": frozenset(ZOTERO_READ_CONTEXT_TOOL_NAMES),
}

_PROVIDER_WRITE_TOOLS: dict[str, frozenset[str]] = {
    "airtable": AIRTABLE_WRITE_ALLOWED_TOOLS | AIRTABLE_TEST_LIFECYCLE_TOOLS,
    "google_workspace": GOOGLE_WORKSPACE_WRITE_TOOLS
    | frozenset({"google_doc_test_lifecycle"}),
    "google_calendar": CALENDAR_WRITE_TOOL_NAMES,
    "gmail": _GMAIL_WRITE_TOOLS,
    "zotero": frozenset(
        {
            *ZOTERO_IMPORT_TOOL_NAMES,
            *ZOTERO_TEST_NOTE_TOOL_NAMES,
            *ZOTERO_TEST_LIBRARY_TOOL_NAMES,
        }
    ),
}

_CALENDAR_WRITE_TOOLS_BY_OPERATION: dict[str, frozenset[str]] = {
    "create": frozenset({"create_google_calendar_event"}),
    "update": frozenset({"update_google_calendar_event"}),
    "delete": frozenset({"delete_google_calendar_event"}),
}

_WORKSPACE_WRITE_TOOLS_BY_OPERATION: dict[str, frozenset[str]] = {
    "create": frozenset(
        {
            "google_doc_write",
            "google_drive_create_folder",
            "google_sheet_append_rows",
            "google_sheet_create",
            "google_sheet_create_tab",
            "google_slide_deck_write",
            "presentation_extract_slide_copy_local",
        }
    ),
    "update": frozenset(
        {
            "google_doc_write",
            "google_drive_rename_folder",
            "google_sheet_update_row",
            "google_sheet_update_tab",
            "google_slide_deck_write",
        }
    ),
    "delete": frozenset(
        {
            "google_doc_trash",
            "google_drive_remove_folder",
            "google_sheet_delete_rows",
            "google_sheet_remove_tab",
            "google_sheet_trash",
            "presentation_delete_test_artifact_local",
        }
    ),
}

_RESEARCH_INTENTS = frozenset(
    {
        "company_research",
        "research_brief",
        "opportunity_search",
        "opportunity_to_outreach_loop",
    }
)
_DRAFT_INTENTS = frozenset({"outreach_draft", "gmail_triage"})
_MUTATING_OPERATIONS = frozenset({"create", "update", "delete", "attach"})
_CONTEXT_AGENT_ROUTES = frozenset(
    {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }
)


def scope_tools_for_request(
    agent_name: str,
    tools: Sequence[Any],
    *,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
    tool_tier: ToolTier | str | int | None = None,
    mode: ToolScopeMode | str = ToolScopeMode.REQUEST_SCOPED,
    required_tool_names: Sequence[str] = (),
) -> ScopedToolAttachment:
    """Select one bounded tool surface without letting raw prose grant tools.

    ``required_tool_names`` is for builder-owned helpers such as a selected
    specialist-as-tool.  It remains constrained by the requested tier and by
    the candidate list; it is not a provider permission bypass.
    """

    normalized_agent = _agent_key(agent_name)
    requested_mode = _normalize_scope_mode(mode)
    candidate_tools = _dedupe_tools(tools)
    candidate_names = tuple(tool_name_for_policy(tool) for tool in candidate_tools)
    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    effective_mode, source = _effective_mode(
        requested_mode,
        authority=authority,
        has_request_plan=manual_request_plan is not None,
    )
    max_tier = normalize_tool_tier(
        tool_tier if tool_tier is not None else default_tool_tier_for_request(manual_request_plan)
    )

    if effective_mode is ToolScopeMode.FULL:
        selected_tools = candidate_tools
        families = (ToolCapabilityFamily.FULL_INSPECTION,)
        notes = (
            (
                "Full tool attachment was explicitly requested."
                if candidate_tools
                else "Full attachment was requested, but the builder exposed no candidate "
                "tools; this is not proof that the request was intentionally tool-free."
            ),
        )
        max_tier_name = "full"
    else:
        allowed_names, families, notes = _allowed_names_for_request(
            normalized_agent,
            authority=authority,
            max_tier=max_tier,
            required_tool_names=required_tool_names,
        )
        selected_tools = tuple(
            tool for tool in candidate_tools if tool_name_for_policy(tool) in allowed_names
        )
        max_tier_name = max_tier.name.lower()

    request_scoped_selected_names = {
        tool_name_for_policy(tool) for tool in selected_tools
    }
    scenario_ceiling = _acceptance_scenario_tool_ceiling(normalized_agent)
    if scenario_ceiling is not None:
        selected_tools = tuple(
            tool
            for tool in selected_tools
            if tool_name_for_policy(tool) in scenario_ceiling
        )
        source = f"{source}+public_acceptance_profile"
        notes = (
            *notes,
            "The enabled public acceptance profile restricted the request to its exact "
            "reviewed function-tool allowlist.",
        )

    selected_names = tuple(tool_name_for_policy(tool) for tool in selected_tools)
    selected_name_set = set(selected_names)
    omitted_names = tuple(
        name for name in candidate_names if name not in selected_name_set
    )
    omission_reasons = tuple(
        reason
        for reason, applies in (
            (
                "request_scope_policy",
                any(name not in request_scoped_selected_names for name in omitted_names),
            ),
            (
                "public_acceptance_profile_allowlist",
                scenario_ceiling is not None
                and any(name in request_scoped_selected_names for name in omitted_names),
            ),
        )
        if applies
    )
    omitted_count = len(omitted_names)
    reduction_ratio = round(omitted_count / len(candidate_tools), 6) if candidate_tools else 0.0
    payload = {
        "agent_name": normalized_agent,
        "effective_mode": effective_mode.value,
        "source": source,
        "max_tool_tier": max_tier_name,
        "capability_families": [family.value for family in families],
        "candidate_tool_names": candidate_names,
        "selected_tool_names": selected_names,
    }
    scope = RequestToolScopeReceipt(
        agent_name=normalized_agent,
        requested_mode=requested_mode,
        effective_mode=effective_mode,
        source=source,
        max_tool_tier=max_tier_name,
        capability_families=families,
        candidate_tool_count=len(candidate_tools),
        candidate_tool_names=candidate_names,
        selected_tool_names=selected_names,
        selected_tool_count=len(selected_tools),
        omitted_tool_count=omitted_count,
        omitted_tool_names=omitted_names,
        omission_reasons=omission_reasons,
        reduction_ratio=reduction_ratio,
        selection_fingerprint=_fingerprint(payload),
        notes=notes,
    )
    return ScopedToolAttachment(tools=selected_tools, scope=scope)


def tool_free_synthesis_attachment(
    agent_name: str,
    candidate_tools: Sequence[Any],
    *,
    source: str = "supplied_context_tool_free",
) -> ScopedToolAttachment:
    """Describe a supplied-context model stage that intentionally receives no tools.

    The receipt retains the registered runtime toolbox as the candidate ceiling so
    observability can distinguish a deliberately tool-free synthesis stage from an
    empty or incompletely inspected builder.
    """

    normalized_agent = _agent_key(agent_name)
    candidates = _dedupe_tools(candidate_tools)
    candidate_names = tuple(tool_name_for_policy(tool) for tool in candidates)
    payload = {
        "agent_name": normalized_agent,
        "effective_mode": ToolScopeMode.REQUEST_SCOPED.value,
        "source": source,
        "max_tool_tier": "none",
        "capability_families": [],
        "candidate_tool_names": candidate_names,
        "selected_tool_names": (),
    }
    scope = RequestToolScopeReceipt(
        agent_name=normalized_agent,
        requested_mode=ToolScopeMode.REQUEST_SCOPED,
        effective_mode=ToolScopeMode.REQUEST_SCOPED,
        source=source,
        max_tool_tier="none",
        candidate_tool_count=len(candidates),
        candidate_tool_names=candidate_names,
        selected_tool_names=(),
        selected_tool_count=0,
        omitted_tool_count=len(candidates),
        omitted_tool_names=candidate_names,
        omission_reasons=("supplied_context_tool_free",) if candidates else (),
        reduction_ratio=1.0 if candidates else 0.0,
        selection_fingerprint=_fingerprint(payload),
        notes=(
            "Deterministic retrieval supplied the bounded context; this model stage may "
            "interpret evidence but cannot call tools or perform side effects.",
        ),
    )
    return ScopedToolAttachment(tools=(), scope=scope)


def _acceptance_scenario_tool_ceiling(agent_name: str) -> frozenset[str] | None:
    """Return the exact reviewed public-canary tool ceiling when one is active."""

    if not str(os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE") or ""):
        return None
    from keystone_agents.canary_acceptance import load_profile

    profile = load_profile()
    if profile is None or not profile.enabled or not profile.is_public_preprint_scenario:
        return None
    return profile.allowed_function_tools(agent_name)


def attach_tool_scope_receipt(agent: Any, scope: RequestToolScopeReceipt) -> Any:
    """Attach non-authoritative inspection metadata to a built SDK agent."""

    try:
        agent.request_tool_scope = scope.receipt()
    except Exception:
        # Some SDK model versions disallow normal assignment but still permit
        # a local diagnostic attribute through object.__setattr__.
        try:
            object.__setattr__(agent, "request_tool_scope", scope.receipt())
        except Exception:
            pass
    return agent


def tool_scope_receipt_for_agent(agent: Any) -> dict[str, Any]:
    """Return the attached request-scope receipt when the builder provided one."""

    value = getattr(agent, "request_tool_scope", None)
    return dict(value) if isinstance(value, Mapping) else {}


def tool_scope_trace_metadata_for_agent(agent: Any) -> dict[str, str | int]:
    """Return a trace-safe compact projection of the request-scope receipt.

    Trace metadata deliberately accepts only short scalar values. The full
    receipt remains in the typed run's request cache; these fields make the
    corresponding trace independently searchable and joinable by fingerprint.
    """

    receipt = tool_scope_receipt_for_agent(agent)
    if not receipt:
        return {}
    families = receipt.get("capability_families") or ()
    return {
        "request_tool_scope_mode": str(receipt.get("effective_mode") or ""),
        "request_tool_scope_source": str(receipt.get("source") or ""),
        "request_tool_scope_max_tier": str(receipt.get("max_tool_tier") or ""),
        "request_tool_scope_families": ",".join(str(item) for item in families),
        "request_tool_scope_candidate_count": int(receipt.get("candidate_tool_count") or 0),
        "request_tool_scope_selected_count": int(receipt.get("selected_tool_count") or 0),
        "request_tool_scope_fingerprint": str(receipt.get("selection_fingerprint") or ""),
    }


def default_tool_tier_for_request(
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None,
) -> str:
    """Return a conservative tier ceiling from validated semantic fields."""

    authority = ExecutionIntentAuthority.from_value(manual_request_plan)
    plan = authority.plan
    if plan is not None and authority.compatibility and not _trusted_compatibility_route(plan):
        plan = None
    if plan is None:
        return "core_read"
    provider = str(plan.provider_system or "").strip()
    operations = authority.effective_provider_operations(provider)
    if (
        (
            plan.requires_durable_state
            and plan.ask_shape.permission_state != "read_only"
        )
        or plan.intent in _DRAFT_INTENTS
        or _MUTATING_OPERATIONS.intersection(operations)
    ):
        return "internal_write"
    if plan.intent == "browser_diagnostics":
        return "diagnostic"
    if (
        plan.target_agent == "business_research_analyst"
        and plan.expected_artifact_type == "source_summary"
        and not plan.requires_live_search
        and plan.ask_shape.permission_state == "read_only"
        and plan.ask_shape.strict_filter_mode in {"exact", "strict"}
    ):
        # Reading one operator-selected public page requires the bounded page
        # extractor even when the user asks in ordinary language rather than
        # describing the work as "deep" research. The exact-source contract
        # below still admits only that extractor, never broad search.
        return "deep_retrieval"
    if (
        plan.target_agent == "opportunity_scout"
        and plan.intent == "opportunity_search"
        and plan.expected_artifact_type == "opportunity_record"
        and plan.requires_live_search
        and plan.ask_shape.permission_state == "read_only"
    ):
        # A live Scout loop needs search, selected-page extraction, and
        # deterministic scoring regardless of whether the operator happens to
        # say "detailed". This is a tool ceiling only; the bounded one-result
        # request can still use the compact prompt and four-turn stage cap.
        return "deep_retrieval"
    if plan.intent in _RESEARCH_INTENTS or plan.requires_live_search:
        return "deep_retrieval" if plan.ask_shape.evidence_depth == "deep" else "web_search"
    return "core_read"


def _allowed_names_for_request(
    agent_name: str,
    *,
    authority: ExecutionIntentAuthority,
    max_tier: ToolTier,
    required_tool_names: Sequence[str],
) -> tuple[frozenset[str], tuple[ToolCapabilityFamily, ...], tuple[str, ...]]:
    plan = authority.plan
    trusted_compatibility = bool(
        plan is not None
        and authority.compatibility
        and _trusted_compatibility_route(plan, agent_name=agent_name)
    )
    if plan is not None and authority.compatibility and not trusted_compatibility:
        plan = None
    route_selected = bool(
        plan is not None
        and (
            authority.requests_route(agent_name)
            or trusted_compatibility
        )
    )
    operational_inspection = bool(
        plan is not None
        and plan.intent == "continue_work_item"
        and plan.provider_system == "unspecified"
        and agent_name in _WORK_ITEM_INSPECTION_BY_AGENT
    )
    selected_source_read = bool(
        plan is not None
        and agent_name in _SELECTED_SOURCE_READ_BY_AGENT
        and plan.expected_artifact_type == "source_summary"
        and not plan.requires_live_search
        and plan.ask_shape.permission_state == "read_only"
        and plan.ask_shape.strict_filter_mode in {"exact", "strict"}
    )
    supplied_opportunity_analysis = bool(
        plan is not None
        and agent_name in _SUPPLIED_OPPORTUNITY_ANALYSIS_BY_AGENT
        and plan.intent == "opportunity_search"
        and plan.expected_artifact_type == "opportunity_record"
        and not plan.requires_live_search
        and plan.ask_shape.permission_state == "read_only"
        and "comparison-format" in plan.constraints
    )
    supplied_outreach_draft = bool(
        plan is not None
        and agent_name in _SUPPLIED_OUTREACH_DRAFT_BY_AGENT
        and plan.intent == "outreach_draft"
        and plan.expected_artifact_type == "outreach_draft"
        and not plan.requires_live_search
        and not plan.requires_approved_context
        and plan.ask_shape.permission_state == "draft_only"
        and plan.ask_shape.prior_context_dependency == "selected_context"
        and "approved_synthetic" in plan.ask_shape.source_type_preference
    )
    minimal_supplied_context = (
        selected_source_read
        or supplied_opportunity_analysis
        or supplied_outreach_draft
    )
    core_names = (
        frozenset()
        if minimal_supplied_context
        else _core_names_for_request(
            agent_name,
            plan=plan,
            operational_inspection=operational_inspection,
        )
    )
    allowed = set(core_names)
    families: list[ToolCapabilityFamily] = (
        [] if minimal_supplied_context else [ToolCapabilityFamily.CORE_CONTEXT]
    )
    notes: list[str] = []

    if plan is None:
        notes.append(
            "No validated semantic plan was available; the toolbox stayed at the "
            "agent-owned core context surface."
        )
    elif route_selected or agent_name in {
        "orchestrator",
        "chief_of_staff",
    }:
        if selected_source_read:
            allowed.update(_SELECTED_SOURCE_READ_BY_AGENT[agent_name])
            families.append(ToolCapabilityFamily.DEEP_RETRIEVAL)
        elif supplied_opportunity_analysis:
            allowed.update(_SUPPLIED_OPPORTUNITY_ANALYSIS_BY_AGENT[agent_name])
            families.append(ToolCapabilityFamily.DEEP_RETRIEVAL)
        elif supplied_outreach_draft:
            allowed.update(_SUPPLIED_OUTREACH_DRAFT_BY_AGENT[agent_name])
            families.append(ToolCapabilityFamily.DRAFT_PREPARATION)
        elif plan.intent in _RESEARCH_INTENTS or plan.requires_live_search:
            if max_tier >= ToolTier.WEB_SEARCH:
                allowed.update(WEB_SEARCH_TOOL_NAMES)
                families.append(ToolCapabilityFamily.PUBLIC_WEB_SEARCH)
            if max_tier >= ToolTier.DEEP_RETRIEVAL:
                allowed.update(DEEP_RETRIEVAL_TOOL_NAMES)
                families.append(ToolCapabilityFamily.DEEP_RETRIEVAL)
        if plan.intent == "browser_diagnostics" and max_tier >= ToolTier.DIAGNOSTIC:
            allowed.update(DIAGNOSTIC_TOOL_NAMES)
            allowed.update(PLAYWRIGHT_RESEARCH_ALLOWED_TOOLS)
            allowed.update(BROWSER_DIAGNOSTIC_ALLOWED_TOOLS)
            families.append(ToolCapabilityFamily.BROWSER_DIAGNOSTICS)

        provider = str(plan.provider_system or "").strip()
        operations = authority.effective_provider_operations(provider)
        if provider and provider != "unspecified" and operations:
            allowed.update(_PROVIDER_READ_TOOLS.get(provider, frozenset()))
            families.append(ToolCapabilityFamily.PROVIDER_READ)
        if (
            not supplied_outreach_draft
            and plan.intent in _DRAFT_INTENTS
            and plan.ask_shape.permission_state in {
            "draft_only",
            "approval_required",
            }
        ):
            allowed.update(_DRAFT_PREPARATION_BY_AGENT.get(agent_name, frozenset()))
            families.append(ToolCapabilityFamily.DRAFT_PREPARATION)
        if (
            plan.requires_durable_state
            and max_tier >= ToolTier.INTERNAL_WRITE
            and plan.ask_shape.permission_state == "approval_required"
        ):
            allowed.update(_DURABLE_STATE_BY_AGENT.get(agent_name, frozenset()))
            families.append(ToolCapabilityFamily.DURABLE_INTERNAL_STATE)
        if (
            _MUTATING_OPERATIONS.intersection(operations)
            and max_tier >= ToolTier.INTERNAL_WRITE
            and plan.ask_shape.permission_state != "read_only"
        ):
            allowed.update(
                _provider_write_tools_for_operations(provider, set(operations))
            )
            families.append(ToolCapabilityFamily.PROVIDER_WRITE)
        notes.append(
            "The validated semantic plan selected capability families; Python policy "
            "and provider gates remain authoritative."
        )
    else:
        notes.append(
            "The semantic plan did not route this agent, so it could not enlarge the core toolbox."
        )

    can_enlarge = bool(
        plan is not None
        and (route_selected or agent_name in {"orchestrator", "chief_of_staff"})
    )
    if can_enlarge:
        for name in required_tool_names:
            normalized = str(name or "").strip()
            if (
                normalized
                and _required_tool_matches_plan(normalized, plan)
                and _within_tier(normalized, max_tier)
            ):
                allowed.add(normalized)
    elif required_tool_names:
        notes.append(
            "Builder-required tools could not enlarge the toolbox without a validated "
            "plan that authorizes this agent."
        )

    # Tier is a hard ceiling even when a capability family contains tools from
    # several classes.  The core set is already explicitly side-effect-free.
    core = core_names
    allowed = {name for name in allowed if name in core or _within_tier(name, max_tier)}
    return (
        frozenset(allowed),
        tuple(dict.fromkeys(families)),
        tuple(notes),
    )


def _required_tool_matches_plan(
    tool_name: str,
    plan: ManualRequestPlan | None,
) -> bool:
    """Keep manager specialist tools within the plan's named owner set.

    Builder-required non-specialist helpers retain their existing behavior.
    Specialist-as-tool candidates are different: a manager may construct the
    registry ceiling, but a request-scoped plan must name the specialist before
    that candidate is admitted.
    """

    if not is_specialist_agent_tool_name(tool_name):
        return True
    if plan is None:
        return False
    route = tool_name.removesuffix(SPECIALIST_AGENT_TOOL_SUFFIX)
    return route in {plan.target_agent, *plan.workflow}


def _provider_write_tools_for_operations(
    provider: str,
    operations: set[str],
) -> frozenset[str]:
    """Return provider write tools bounded to exact typed operations."""

    if provider == "google_calendar":
        return frozenset(
            tool_name
            for operation in operations
            for tool_name in _CALENDAR_WRITE_TOOLS_BY_OPERATION.get(
                operation,
                frozenset(),
            )
        )
    if provider == "google_workspace":
        selected = {
            tool_name
            for operation in operations
            for tool_name in _WORKSPACE_WRITE_TOOLS_BY_OPERATION.get(
                operation,
                frozenset(),
            )
        }
        if {"create", "update", "delete"} <= operations:
            selected.add("google_doc_test_lifecycle")
        return frozenset(selected)
    return _PROVIDER_WRITE_TOOLS.get(provider, frozenset())


def _core_names_for_request(
    agent_name: str,
    *,
    plan: ManualRequestPlan | None,
    operational_inspection: bool,
) -> frozenset[str]:
    """Select the smallest manager-owned context surface for one plan."""

    if operational_inspection:
        return _WORK_ITEM_INSPECTION_BY_AGENT[agent_name]
    if (
        agent_name == "chief_of_staff"
        and plan is not None
        and plan.target_agent == "chief_of_staff"
        and plan.workflow
        and plan.provider_system == "unspecified"
        and plan.ask_shape.prior_context_dependency == "selected_context"
    ):
        return frozenset({"inspect_active_work_items"})
    return _CORE_CONTEXT_BY_AGENT.get(agent_name, frozenset())


def _effective_mode(
    requested: ToolScopeMode,
    *,
    authority: ExecutionIntentAuthority,
    has_request_plan: bool,
) -> tuple[ToolScopeMode, str]:
    if requested is ToolScopeMode.FULL:
        return ToolScopeMode.FULL, "explicit_full"
    if requested is ToolScopeMode.REQUEST_SCOPED:
        return (
            ToolScopeMode.REQUEST_SCOPED,
            "canonical_plan" if authority.canonical else "default_minimum",
        )
    if not has_request_plan:
        return ToolScopeMode.FULL, "legacy_empty_builder_inspection"
    return (
        ToolScopeMode.REQUEST_SCOPED,
        "canonical_plan" if authority.canonical else "default_minimum",
    )


def _normalize_scope_mode(value: ToolScopeMode | str) -> ToolScopeMode:
    if isinstance(value, ToolScopeMode):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"", "scoped", "request"}:
        return ToolScopeMode.REQUEST_SCOPED
    try:
        return ToolScopeMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in ToolScopeMode)
        raise ValueError(
            f"Unsupported tool scope mode {value!r}; expected one of: {allowed}."
        ) from exc


def _trusted_compatibility_route(
    plan: ManualRequestPlan,
    *,
    agent_name: str | None = None,
) -> bool:
    """Accept only the named deterministic preflight plan for toolbox enlargement."""

    requested = str(plan.requested_agent or "").strip()
    target = str(plan.target_agent or "").strip()
    if not requested or requested == "unspecified" or not target:
        return False
    if _trusted_public_web_research_delegation(
        plan,
        requested=requested,
        target=target,
        agent_name=agent_name,
    ):
        return True
    if agent_name is not None:
        if requested == agent_name and target == agent_name:
            return True
        provider_for_agent = {
            "airtable_context_agent": "airtable",
            "google_workspace_context_agent": "google_workspace",
            "zotero_context_agent": "zotero",
        }.get(agent_name)
        operations = set(plan.provider_operations)
        return bool(
            target == agent_name
            and provider_for_agent is not None
            and plan.provider_system == provider_for_agent
            and plan.intent == "context_lookup"
            and operations
            and operations <= {"read", "search", "verify"}
            and plan.side_effect_policy == "draft_or_read_only"
            and plan.ask_shape.permission_state in {"read_only", "unspecified"}
        )
    return requested == target


def _trusted_public_web_research_delegation(
    plan: ManualRequestPlan,
    *,
    requested: str,
    target: str,
    agent_name: str | None,
) -> bool:
    """Trust one read-only context-specialist delegation to public research.

    The full request remains in ``plan.objective``.  This exception only lets
    the selected Business Research agent receive its normal bounded search and
    extraction tools after deterministic preflight has moved an explicitly
    public-web request away from a named context specialist.  It never grants a
    provider mutation or treats a generic compatibility plan as authority.
    """

    if requested not in _CONTEXT_AGENT_ROUTES:
        return False
    if target != "business_research_analyst":
        return False
    if agent_name is not None and agent_name != target:
        return False
    if plan.intent not in _RESEARCH_INTENTS:
        return False
    if plan.task_objective != "source_research":
        return False
    if plan.expected_artifact_type not in {"research_brief", "source_summary"}:
        return False
    if not plan.requires_live_search or plan.provider_system != "unspecified":
        return False
    if set(plan.provider_operations).difference({"read", "search", "verify"}):
        return False
    if plan.side_effect_policy != "draft_or_read_only":
        return False
    if plan.ask_shape.permission_state not in {"read_only", "unspecified"}:
        return False
    if request_forbids_live_research(plan.objective):
        return False
    return looks_like_explicit_public_web_research(plan.objective)


def _within_tier(tool_name: str, max_tier: ToolTier) -> bool:
    tier = tool_tier_for_name(tool_name)
    return tier is not None and tier <= max_tier


def _dedupe_tools(tools: Sequence[Any]) -> tuple[Any, ...]:
    deduped: list[Any] = []
    seen: set[str] = set()
    for tool in tools:
        name = tool_name_for_policy(tool)
        if name in seen:
            continue
        seen.add(name)
        deduped.append(tool)
    return tuple(deduped)


def _agent_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "RequestToolScopeReceipt",
    "ScopedToolAttachment",
    "ToolCapabilityFamily",
    "ToolScopeMode",
    "attach_tool_scope_receipt",
    "default_tool_tier_for_request",
    "scope_tools_for_request",
    "tool_free_synthesis_attachment",
    "tool_scope_receipt_for_agent",
]
