"""Repo-local skill bundles available to Keystone agents.

Skills are prompt-time reasoning and output contracts. They do not attach tools,
grant permissions, route requests, or execute side effects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

SHARED_REASONING_SKILL_NAMES = (
    "ask_to_target_resolution",
    "artifact_evidence_handling",
    "data_schema_mapping",
    "identity_and_record_resolution",
    "prior_work_and_duplicate_checking",
    "evidence_attribution_and_claim_mapping",
    "source_triage_decision",
    "context_permission_gating",
    "action_boundary_enforcement",
    "unsupported_claim_and_gap_handling",
    "tool_result_resilience",
    "structured_output_quality_review",
    "workspace_artifact_governance",
    "workflow_lifecycle_tracking",
    "handoff_contract_packaging",
)

CORE_SKILL_NAMES = (
    "context_permission_gating",
    "action_boundary_enforcement",
    "tool_result_resilience",
    "structured_output_quality_review",
)

SPECIALIST_SKILL_NAMES = {
    "gmail_triage": "gmail_triage_specialist_contracts",
    "business_research_analyst": "business_research_specialist_contracts",
    "opportunity_scout": "opportunity_scout_specialist_contracts",
    "outreach_composer": "outreach_composer_specialist_contracts",
    "airtable_context_agent": "airtable_context_specialist_contracts",
    "google_workspace_context_agent": "google_workspace_context_specialist_contracts",
    "zotero_context_agent": "zotero_context_specialist_contracts",
    "rss_context_agent": "rss_context_specialist_contracts",
    "preprints_context_agent": "preprints_context_specialist_contracts",
    "orchestrator": "orchestrator_specialist_contracts",
    "chief_of_staff": "chief_of_staff_specialist_contracts",
}

AGENT_SKILL_NAMES: dict[str, tuple[str, ...]] = {
    "gmail_triage": (
        *SHARED_REASONING_SKILL_NAMES,
        "writing_style_adaptation",
        "gmail_triage_specialist_contracts",
    ),
    "business_research_analyst": (
        *SHARED_REASONING_SKILL_NAMES,
        "business_research_specialist_contracts",
    ),
    "opportunity_scout": (
        *SHARED_REASONING_SKILL_NAMES,
        "opportunity_scout_specialist_contracts",
    ),
    "outreach_composer": (
        *SHARED_REASONING_SKILL_NAMES,
        "writing_style_adaptation",
        "outreach_composer_specialist_contracts",
    ),
    "airtable_context_agent": (
        "ask_to_target_resolution",
        "artifact_evidence_handling",
        "data_schema_mapping",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "airtable_context_specialist_contracts",
    ),
    "google_workspace_context_agent": (
        "artifact_evidence_handling",
        "data_schema_mapping",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "workspace_artifact_governance",
        "google_workspace_context_specialist_contracts",
    ),
    "zotero_context_agent": (
        "artifact_evidence_handling",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "workspace_artifact_governance",
        "zotero_context_specialist_contracts",
    ),
    "rss_context_agent": (
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "rss_context_specialist_contracts",
    ),
    "preprints_context_agent": (
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "unsupported_claim_and_gap_handling",
        "tool_result_resilience",
        "structured_output_quality_review",
        "preprints_context_specialist_contracts",
    ),
    "orchestrator": (
        *SHARED_REASONING_SKILL_NAMES,
        "request_to_specialist_brief",
        "orchestrator_specialist_contracts",
    ),
    "chief_of_staff": (
        *SHARED_REASONING_SKILL_NAMES,
        "chief_of_staff_specialist_contracts",
    ),
}

DEFAULT_ROUTE_SKILL_NAMES: dict[str, tuple[str, ...]] = {
    "gmail_triage": (
        "identity_and_record_resolution",
        "prior_work_and_duplicate_checking",
        "workflow_lifecycle_tracking",
        "handoff_contract_packaging",
        "writing_style_adaptation",
    ),
    "business_research_analyst": (
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "unsupported_claim_and_gap_handling",
        "handoff_contract_packaging",
    ),
    "opportunity_scout": (
        "identity_and_record_resolution",
        "prior_work_and_duplicate_checking",
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "unsupported_claim_and_gap_handling",
        "workflow_lifecycle_tracking",
        "handoff_contract_packaging",
    ),
    "outreach_composer": (
        "identity_and_record_resolution",
        "prior_work_and_duplicate_checking",
        "evidence_attribution_and_claim_mapping",
        "unsupported_claim_and_gap_handling",
        "workflow_lifecycle_tracking",
        "handoff_contract_packaging",
        "writing_style_adaptation",
    ),
    "airtable_context_agent": (
        "ask_to_target_resolution",
        "artifact_evidence_handling",
        "data_schema_mapping",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "airtable_context_specialist_contracts",
    ),
    "google_workspace_context_agent": (
        "artifact_evidence_handling",
        "data_schema_mapping",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "workspace_artifact_governance",
        "google_workspace_context_specialist_contracts",
    ),
    "zotero_context_agent": (
        "artifact_evidence_handling",
        "identity_and_record_resolution",
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "workspace_artifact_governance",
        "zotero_context_specialist_contracts",
    ),
    "rss_context_agent": (
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "tool_result_resilience",
        "structured_output_quality_review",
        "rss_context_specialist_contracts",
    ),
    "preprints_context_agent": (
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "context_permission_gating",
        "action_boundary_enforcement",
        "unsupported_claim_and_gap_handling",
        "tool_result_resilience",
        "structured_output_quality_review",
        "preprints_context_specialist_contracts",
    ),
    "orchestrator": (
        "request_to_specialist_brief",
        "identity_and_record_resolution",
        "prior_work_and_duplicate_checking",
        "evidence_attribution_and_claim_mapping",
        "source_triage_decision",
        "unsupported_claim_and_gap_handling",
        "workflow_lifecycle_tracking",
        "handoff_contract_packaging",
    ),
    "chief_of_staff": (
        "ask_to_target_resolution",
        "artifact_evidence_handling",
        "identity_and_record_resolution",
        "prior_work_and_duplicate_checking",
        "source_triage_decision",
        "workspace_artifact_governance",
        "workflow_lifecycle_tracking",
        "handoff_contract_packaging",
    ),
}

CONDITIONAL_SKILL_TRIGGERS: dict[str, tuple[str, ...]] = {
    "identity_and_record_resolution": (
        "account",
        "artifact",
        "company",
        "contact",
        "dedupe",
        "domain",
        "duplicate",
        "entity",
        "identity",
        "match",
        "merge",
        "person",
        "record",
        "same",
        "source id",
        "thread",
        "workitem",
        "work item",
    ),
    "prior_work_and_duplicate_checking": (
        "again",
        "already",
        "continue",
        "duplicate",
        "existing",
        "follow up",
        "follow-up",
        "previous",
        "prior",
        "resume",
        "same draft",
    ),
    "evidence_attribution_and_claim_mapping": (
        "article",
        "citation",
        "cite",
        "claim",
        "evidence",
        "fact",
        "public",
        "research",
        "source",
        "url",
        "website",
    ),
    "source_triage_decision": (
        "deep search",
        "deeper search",
        "provider",
        "providers",
        "retrieval",
        "search",
        "source candidate",
        "source candidates",
        "source-backed",
        "source backed",
        "sources",
        "tavily",
        "exa",
        "searxng",
        "web search",
    ),
    "unsupported_claim_and_gap_handling": (
        "assumption",
        "gap",
        "missing",
        "thin",
        "unsupported",
        "unverified",
        "validate",
        "verification",
    ),
    "workspace_artifact_governance": (
        "artifact",
        "doc",
        "docs",
        "drive",
        "google sheet",
        "kniops",
        "publish",
        "report",
        "save",
        "sheet",
        "spreadsheet",
        "workspace",
    ),
    "artifact_evidence_handling": (
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        "attachment",
        "attached",
        "artifact",
        "document",
        "file",
        "image",
        "invoice",
        "local path",
        "pdf",
        "receipt",
        "screenshot",
        "upload",
    ),
    "data_schema_mapping": (
        ".csv",
        ".pdf",
        ".png",
        ".xlsx",
        "airtable",
        "base",
        "business expense",
        "business expenses",
        "column",
        "columns",
        "data mapping",
        "estimated period",
        "estimated tax period",
        "field",
        "fields",
        "fill",
        "form",
        "invoice",
        "map",
        "mapping",
        "personal expense",
        "personal expenses",
        "populate",
        "receipt",
        "record",
        "schema",
        "sheet",
        "structured data",
        "table",
        "tax period",
    ),
    "workflow_lifecycle_tracking": (
        "approval",
        "approved",
        "draft",
        "follow up",
        "follow-up",
        "next step",
        "outcome",
        "pending",
        "rejected",
        "reply",
        "sent",
        "status",
        "workitem",
        "work item",
    ),
    "handoff_contract_packaging": (
        "delegate",
        "handoff",
        "next agent",
        "orchestrator",
        "research then draft",
        "route",
        "specialist",
    ),
    "writing_style_adaptation": (
        "compose",
        "draft",
        "email",
        "linkedin",
        "message",
        "reply",
        "style",
        "tone",
        "voice",
        "writing",
    ),
    "request_to_specialist_brief": (
        "brief",
        "constraints",
        "exact match",
        "exact-match",
        "hard filters",
        "packet",
        "short prompt",
        "stop after",
    ),
}

CONTEXT_FLAG_SKILLS: dict[str, tuple[str, ...]] = {
    "needs_identity_resolution": ("identity_and_record_resolution",),
    "needs_duplicate_check": ("prior_work_and_duplicate_checking",),
    "needs_source_attribution": ("evidence_attribution_and_claim_mapping",),
    "needs_source_triage": ("source_triage_decision",),
    "needs_unsupported_claim_review": ("unsupported_claim_and_gap_handling",),
    "needs_workspace_artifact": ("workspace_artifact_governance",),
    "needs_artifact_evidence": ("artifact_evidence_handling",),
    "needs_lifecycle_tracking": ("workflow_lifecycle_tracking",),
    "needs_handoff": ("handoff_contract_packaging",),
}


def skill_request_text(value: Any) -> str:
    """Return compact text used only for deterministic skill selection."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump_json"):
        try:
            return str(value.model_dump_json())
        except TypeError:
            pass
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    return str(value)


def _ordered_subset(agent_name: str, selected: set[str]) -> tuple[str, ...]:
    return tuple(skill for skill in AGENT_SKILL_NAMES[agent_name] if skill in selected)


def _add_reason(
    reasons: dict[str, list[str]],
    skill_name: str,
    reason: str,
) -> None:
    reasons.setdefault(skill_name, [])
    if reason not in reasons[skill_name]:
        reasons[skill_name].append(reason)


def explain_agent_skill_selection(
    agent_name: str,
    *,
    request_text: str | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_all: bool = False,
) -> dict[str, tuple[str, ...]]:
    """Return selected skills with deterministic selection reasons."""

    if agent_name not in AGENT_SKILL_NAMES:
        raise KeyError(f"Unknown Keystone agent skill bundle: {agent_name}")
    if include_all:
        return {skill_name: ("include_all",) for skill_name in AGENT_SKILL_NAMES[agent_name]}

    reasons: dict[str, list[str]] = {}
    for skill_name in CORE_SKILL_NAMES:
        _add_reason(reasons, skill_name, "core")
    _add_reason(reasons, SPECIALIST_SKILL_NAMES[agent_name], "specialist")
    for skill_name in DEFAULT_ROUTE_SKILL_NAMES.get(agent_name, ()):
        _add_reason(reasons, skill_name, "route_default")

    lowered = (request_text or "").lower()
    for skill_name, triggers in CONDITIONAL_SKILL_TRIGGERS.items():
        for trigger in triggers:
            if trigger in lowered:
                _add_reason(reasons, skill_name, f"request_trigger:{trigger}")
                break
    if context_flags:
        for flag, skill_names in CONTEXT_FLAG_SKILLS.items():
            if context_flags.get(flag):
                for skill_name in skill_names:
                    _add_reason(reasons, skill_name, f"context_flag:{flag}")

    return {
        skill_name: tuple(reasons[skill_name])
        for skill_name in _ordered_subset(agent_name, set(reasons))
    }


def select_agent_skill_names(
    agent_name: str,
    *,
    request_text: str | None = None,
    context_flags: Mapping[str, bool] | None = None,
    include_all: bool = False,
) -> tuple[str, ...]:
    """Select the deterministic runtime skill subset for an agent run.

    The selector controls which skill contracts are visible to the model. It does
    not decide how the model reasons inside those contracts, grant tools, or
    bypass Python safety gates.
    """

    return tuple(
        explain_agent_skill_selection(
            agent_name,
            request_text=request_text,
            context_flags=context_flags,
            include_all=include_all,
        )
    )
