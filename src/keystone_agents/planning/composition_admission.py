"""Resolve provider-free composition from typed plan and Slack provenance."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.schemas.composition_admission import (
    ProviderFreeCompositionAdmission,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

_COMPLETED_STATUSES = frozenset({"completed", "done", "recovered", "success"})
_SUPPORTED_SOURCE_ROUTES = frozenset(
    {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    }
)


def is_provider_free_selected_context_draft_plan(plan: ManualRequestPlan) -> bool:
    """Return whether a plan requests only show-only composition."""

    return bool(
        plan.target_agent == "outreach_composer"
        and plan.intent in {"route_request", "outreach_draft"}
        and plan.ask_shape.prior_context_dependency == "selected_context"
        and plan.ask_shape.permission_state == "draft_only"
        and not plan.workflow
        and not plan.requires_durable_state
        and not plan.requires_live_search
        and plan.provider_system == "unspecified"
        and not plan.provider_operations
        and plan.side_effect_policy == "draft_or_read_only"
    )


def resolve_provider_free_composition_admission(
    plan: ManualRequestPlan,
    *,
    workflow_state: Mapping[str, Any] | None,
) -> ProviderFreeCompositionAdmission:
    """Admit selected prior evidence without granting provider or external use."""

    if not is_provider_free_selected_context_draft_plan(plan):
        return ProviderFreeCompositionAdmission(
            reason="plan_not_provider_free_composition"
        )
    prior_runs = (
        workflow_state.get("prior_agent_runs")
        if isinstance(workflow_state, Mapping)
        else None
    )
    if not isinstance(prior_runs, list) or not prior_runs:
        return ProviderFreeCompositionAdmission(reason="selected_context_missing")

    saw_same_thread = False
    saw_completed = False
    for item in reversed(prior_runs):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("thread_correlation") or "").strip() != "same_thread":
            continue
        saw_same_thread = True
        status = str(item.get("status") or "").strip().lower()
        if status not in _COMPLETED_STATUSES:
            continue
        saw_completed = True
        route = str(item.get("route") or "").strip().lower()
        if route not in _SUPPORTED_SOURCE_ROUTES:
            continue
        summary = str(item.get("summary") or item.get("title") or "").strip()
        if not summary:
            continue
        return ProviderFreeCompositionAdmission(
            composition_allowed=True,
            same_thread_verified=True,
            source_route=route,
            source_run_id=str(item.get("id") or "").strip(),
            context_kind=(
                "selected_email_reply_context"
                if route == "gmail_triage"
                else "selected_outreach_context"
            ),
            reason="admitted",
        )

    if not saw_same_thread:
        reason = "selected_context_not_same_thread"
    elif not saw_completed:
        reason = "selected_context_not_completed"
    else:
        reason = "selected_context_route_not_supported"
    return ProviderFreeCompositionAdmission(
        same_thread_verified=saw_same_thread,
        reason=reason,
    )

