"""Resolve provider-free composition from typed plan and Slack provenance."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from keystone_agents.instruction_following import (
    canonical_source_url,
    canonical_source_urls,
)
from keystone_agents.schemas.composition_admission import (
    ProviderFreeCompositionAdmission,
    VerifiedSignalContext,
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
_SIGNAL_SOURCE_ROUTES = frozenset({"rss_context_agent", "preprints_context_agent"})
CONTEXT_ONLY_RESPONSE_ROUTES = (
    _SUPPORTED_SOURCE_ROUTES | _SIGNAL_SOURCE_ROUTES | {"chief_of_staff"}
)
_CONTEXT_ONLY_SOURCE_ROUTES = CONTEXT_ONLY_RESPONSE_ROUTES


def _verified_signal_context(
    workflow_state: Mapping[str, Any] | None,
) -> VerifiedSignalContext | None:
    if not isinstance(workflow_state, Mapping):
        return None
    value = workflow_state.get("verified_signal_context")
    if isinstance(value, VerifiedSignalContext):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        return VerifiedSignalContext.model_validate(value)
    except (TypeError, ValueError):
        return None


def _selected_signal_context_for_plan(
    context: VerifiedSignalContext,
    plan: ManualRequestPlan,
) -> tuple[VerifiedSignalContext | None, str]:
    """Resolve an exact bounded subset without interpreting descriptive prose."""

    exact_selectors = [
        str(value or "").strip()
        for value in [plan.primary_target, *plan.required_entities]
        if str(value or "").strip()
    ]
    selected_ids, explicit_reference, mismatch = explicit_signal_source_selection(
        context,
        texts=(str(plan.objective or ""),),
        exact_selectors=exact_selectors,
    )
    if explicit_reference:
        if mismatch or not selected_ids:
            return None, "selected_context_explicit_source_mismatch"
    elif len(context.selected_sources) == 1:
        return context, "admitted"
    else:
        return None, "selected_context_ambiguous"
    selected_id_set = set(selected_ids)
    return context.model_copy(
        update={
            "selected_sources": [
                source
                for source in context.selected_sources
                if source.source_id in selected_id_set
            ],
            "source_dates": {
                source_id: published_at
                for source_id, published_at in context.source_dates.items()
                if source_id in selected_id_set
            },
            "interpretations": [
                item
                for item in context.interpretations
                if item.source_id in selected_id_set
            ],
        }
    ), "admitted"


def explicit_signal_source_selection(
    context: VerifiedSignalContext,
    *,
    texts: tuple[str, ...] = (),
    exact_selectors: list[str] | tuple[str, ...] = (),
) -> tuple[list[str], bool, bool]:
    """Return exact selected IDs, whether identity was explicit, and mismatch state."""

    canonical_by_id = {
        source.source_id: canonical_source_url(source.url)
        for source in context.selected_sources
    }
    id_by_url = {
        canonical: source_id
        for source_id, canonical in canonical_by_id.items()
        if canonical
    }
    visible_urls = {
        url
        for text in texts
        for url in canonical_source_urls(text)
    }
    selected_ids: set[str] = set()
    mismatch = bool(visible_urls.difference(id_by_url))
    selected_ids.update(id_by_url[url] for url in visible_urls.intersection(id_by_url))
    explicit_reference = bool(visible_urls)

    for selector in exact_selectors:
        canonical = canonical_source_url(selector)
        if canonical:
            explicit_reference = True
            if canonical in id_by_url:
                selected_ids.add(id_by_url[canonical])
            else:
                mismatch = True
        elif selector in canonical_by_id:
            explicit_reference = True
            selected_ids.add(selector)

    combined_text = "\n".join(str(text or "") for text in texts)
    for source_id in canonical_by_id:
        if re.search(
            rf"(?<![\w-]){re.escape(source_id)}(?![\w-])",
            combined_text,
        ):
            explicit_reference = True
            selected_ids.add(source_id)
    return (
        [
            source.source_id
            for source in context.selected_sources
            if source.source_id in selected_ids
        ],
        explicit_reference,
        mismatch,
    )


def is_context_only_response_plan(plan: ManualRequestPlan) -> bool:
    """Recognize the narrow model-owned answer mode, never provider authority."""
    return bool(
        plan.source == "canonical:orchestrator_context_only"
        and plan.target_agent in CONTEXT_ONLY_RESPONSE_ROUTES
        and plan.intent == "route_request"
        and plan.task_objective == "route_or_continue"
        and plan.expected_artifact_type == "none"
        and plan.ask_shape.prior_context_dependency == "selected_context"
        and not plan.workflow and not plan.requires_durable_state
        and not plan.requires_live_search and plan.provider_system == "unspecified"
        and not plan.provider_operations and not plan.provider_action_steps
        and plan.side_effect_policy == "draft_or_read_only"
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


def is_selected_public_url_read_plan(plan: ManualRequestPlan) -> bool:
    """Return whether the plan authorizes one bounded public-URL provider read."""

    provider_operations = {
        str(value or "").strip().lower() for value in plan.provider_operations
    }
    direct_research_stage = bool(
        plan.target_agent == "business_research_analyst"
        and plan.task_objective == "source_research"
    )
    staged_research_workflow = bool(
        "business_research_analyst" in plan.workflow
        and plan.ask_shape.prior_context_dependency == "selected_context"
        and plan.ask_shape.permission_state
        in {"read_only", "draft_only", "approval_required"}
        and plan.ask_shape.strict_filter_mode in {"exact", "strict"}
        and not plan.requires_live_search
    )
    return bool(
        (direct_research_stage or staged_research_workflow)
        and plan.target_type == "url"
        and plan.primary_target.startswith(("http://", "https://"))
        and "read" in provider_operations
        and not provider_operations.difference({"read"})
    )


def resolve_provider_free_composition_admission(
    plan: ManualRequestPlan,
    *,
    workflow_state: Mapping[str, Any] | None,
) -> ProviderFreeCompositionAdmission:
    """Admit selected prior evidence without granting provider or external use."""

    context_only = is_context_only_response_plan(plan)
    if not (is_provider_free_selected_context_draft_plan(plan) or context_only):
        return ProviderFreeCompositionAdmission(
            reason="plan_not_provider_free_composition"
        )
    if not context_only and "approved_synthetic" in plan.ask_shape.source_type_preference:
        return ProviderFreeCompositionAdmission(
            composition_allowed=True,
            context_kind="operator_approved_synthetic_facts",
            reason="admitted_approved_synthetic_context",
        )
    prior_runs = (
        workflow_state.get("prior_agent_runs")
        if isinstance(workflow_state, Mapping)
        else None
    )
    signal_history = (
        workflow_state.get("signal_run_history")
        if isinstance(workflow_state, Mapping)
        else None
    )
    signal_context = _verified_signal_context(workflow_state)
    if _signal_history_is_selected(
        plan,
        signal_context=signal_context,
        signal_history=signal_history,
        prior_runs=prior_runs,
    ):
        return _signal_history_admission(
            plan,
            signal_context=signal_context,
            signal_history=signal_history,
        )
    if not isinstance(prior_runs, list) or not prior_runs:
        return ProviderFreeCompositionAdmission(reason="selected_context_missing")

    saw_same_thread = False
    saw_completed = False
    signal_failure_reason = ""
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
        supported_routes = (
            _CONTEXT_ONLY_SOURCE_ROUTES if context_only else _SUPPORTED_SOURCE_ROUTES
        )
        if route not in supported_routes:
            continue
        if route in _SIGNAL_SOURCE_ROUTES:
            if signal_context is None:
                signal_failure_reason = "selected_context_source_missing"
                continue
            if signal_context.verification_status != "verified":
                signal_failure_reason = "selected_context_source_unverified"
                continue
            if (
                signal_context.source_route != route
                or signal_context.source_run_id != str(item.get("id") or "").strip()
            ):
                signal_failure_reason = "selected_context_source_mismatch"
                continue
            selected_signal_context, selection_reason = _selected_signal_context_for_plan(
                signal_context,
                plan,
            )
            if selected_signal_context is None:
                signal_failure_reason = selection_reason
                continue
            return ProviderFreeCompositionAdmission(
                composition_allowed=True,
                same_thread_verified=True,
                source_route=route,
                source_run_id=signal_context.source_run_id,
                context_kind="verified_signal_context",
                verified_signal_context=selected_signal_context,
                reason="admitted",
            )
        summary = str(
            item.get("summary") or (item.get("title") if not context_only else "") or ""
        ).strip()
        if not summary:
            continue
        return ProviderFreeCompositionAdmission(
            composition_allowed=True,
            same_thread_verified=True,
            source_route=route,
            source_run_id=str(item.get("id") or "").strip(),
            context_kind=(
                "selected_thread_response" if context_only else
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
    elif signal_failure_reason:
        reason = signal_failure_reason
    else:
        reason = "selected_context_route_not_supported"
    return ProviderFreeCompositionAdmission(
        same_thread_verified=saw_same_thread,
        reason=reason,
    )


def _signal_history_is_selected(
    plan: ManualRequestPlan,
    *,
    signal_context: VerifiedSignalContext | None,
    signal_history: object,
    prior_runs: object,
) -> bool:
    if not isinstance(signal_history, list) or not signal_history:
        return False
    if signal_context is not None:
        _ids, explicit, _mismatch = explicit_signal_source_selection(
            signal_context,
            texts=(str(plan.objective or ""),),
            exact_selectors=[plan.primary_target, *plan.required_entities],
        )
        if explicit or signal_context.selection_basis == "explicit_older_signal":
            return True
    if not isinstance(prior_runs, list) or not prior_runs:
        return True
    latest_prior = next(
        (item for item in reversed(prior_runs) if isinstance(item, Mapping)),
        {},
    )
    return str(latest_prior.get("route") or "").strip().lower() in _SIGNAL_SOURCE_ROUTES


def _signal_history_admission(
    plan: ManualRequestPlan,
    *,
    signal_context: VerifiedSignalContext | None,
    signal_history: object,
) -> ProviderFreeCompositionAdmission:
    history = [
        item
        for item in signal_history
        if isinstance(item, Mapping)
        and str(item.get("thread_correlation") or "") == "same_thread"
    ]
    if not history:
        return ProviderFreeCompositionAdmission(reason="selected_context_not_same_thread")
    latest = history[0]
    latest_status = str(latest.get("status") or "").strip().lower()
    if signal_context is None:
        return ProviderFreeCompositionAdmission(
            same_thread_verified=True,
            reason=(
                "selected_context_latest_signal_incomplete"
                if latest_status not in _COMPLETED_STATUSES
                or latest.get("completion_confirmed") is not True
                else "selected_context_source_unverified"
            ),
        )
    source_index = next(
        (
            index
            for index, item in enumerate(history)
            if str(item.get("id") or "") == signal_context.source_run_id
            and str(item.get("route") or "") == signal_context.source_route
            and item.get("verified_context_available") is True
        ),
        None,
    )
    if source_index is None:
        return ProviderFreeCompositionAdmission(
            same_thread_verified=True,
            reason="selected_context_source_mismatch",
        )
    if source_index == 0 and signal_context.selection_basis != "latest_verified_signal":
        return ProviderFreeCompositionAdmission(
            same_thread_verified=True,
            reason="selected_context_source_mismatch",
        )
    if source_index > 0 and (
        signal_context.selection_basis != "explicit_older_signal"
        or signal_context.newer_signal_run_id != str(latest.get("id") or "")
        or signal_context.newer_signal_request_ts != str(latest.get("request_ts") or "")
        or signal_context.newer_signal_status != latest_status
    ):
        return ProviderFreeCompositionAdmission(
            same_thread_verified=True,
            reason="selected_context_latest_signal_incomplete",
        )
    selected_context, selection_reason = _selected_signal_context_for_plan(
        signal_context,
        plan,
    )
    if selected_context is None:
        return ProviderFreeCompositionAdmission(
            same_thread_verified=True,
            reason=selection_reason,
        )
    return ProviderFreeCompositionAdmission(
        composition_allowed=True,
        same_thread_verified=True,
        source_route=selected_context.source_route,
        source_run_id=selected_context.source_run_id,
        context_kind="verified_signal_context",
        verified_signal_context=selected_context,
        reason="admitted",
    )
