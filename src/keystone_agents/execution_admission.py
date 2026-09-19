"""Shared admission contract for natural-language provider actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

AdmissionMode = Literal[
    "typed_control",
    "provider_action",
    "semantic_planning",
]


@dataclass(frozen=True)
class ExecutionAdmission:
    """Explain why a request may bypass or continue past semantic planning.

    Provider words and action verbs are not authority. Natural language reaches
    a provider action only when the shared semantic plan names the provider,
    owner, and supported intent. Exact typed controls may bypass semantic
    planning because their operation and target were established by the control
    contract rather than inferred from prose.
    """

    mode: AdmissionMode
    provider: str = ""
    intent_source: str = ""
    positive_intent: bool = False
    provider_action_bound: bool = False
    can_execute_provider_action: bool = False
    can_block_for_missing_fields: bool = False
    reason: str = ""


def admit_provider_action(
    *,
    provider: str,
    provider_action_bound: bool,
    semantic_plan: ManualRequestPlan | None,
    allowed_agents: set[str] | frozenset[str],
    allowed_intents: set[str] | frozenset[str] = frozenset(
        {"business_system_write"}
    ),
    typed_control: bool = False,
    dry_run_preview: bool = False,
) -> ExecutionAdmission:
    """Admit provider work from semantic meaning, then validate exact scope.

    ``provider_action_bound`` is retained as diagnostic compatibility metadata;
    it cannot veto or grant execution. Provider handlers validate required
    fields, exact object identity, approval/live gates, and receipts after this
    semantic admission.
    """

    clean_provider = str(provider or "").strip()
    if typed_control:
        return ExecutionAdmission(
            mode="typed_control",
            provider=clean_provider,
            intent_source="typed_control",
            positive_intent=True,
            provider_action_bound=True,
            can_execute_provider_action=True,
            can_block_for_missing_fields=True,
            reason=(
                "An exact typed control established the provider operation and "
                "target contract."
            ),
        )

    authority = ExecutionIntentAuthority.from_value(semantic_plan)
    plan = authority.plan
    plan_intent = str(getattr(plan, "intent", "") or "").strip()
    plan_agent = str(getattr(plan, "target_agent", "") or "").strip()
    plan_provider = str(getattr(plan, "provider_system", "") or "").strip()
    operations = set(authority.effective_provider_operations(clean_provider))
    permission_state = str(
        getattr(getattr(plan, "ask_shape", None), "permission_state", "unspecified")
        or "unspecified"
    )
    operation_shape_allowed = bool(
        (
            plan_intent == "business_system_write"
            and bool(operations.intersection({"create", "update", "delete", "attach"}))
            and permission_state != "read_only"
        )
        or (
            plan_intent == "context_lookup"
            and bool(operations)
            and not operations.difference({"read", "search", "verify"})
        )
    )
    canonical_positive_intent = bool(
        authority.canonical
        and plan is not None
        and plan_provider == clean_provider
        and plan_intent in allowed_intents
        and plan_agent in allowed_agents
        and operation_shape_allowed
    )
    compatibility_preview_intent = bool(
        dry_run_preview
        and plan is not None
        and plan_provider == clean_provider
        and plan_intent in allowed_intents
        and plan_agent in allowed_agents
        and operation_shape_allowed
    )
    positive_intent = canonical_positive_intent or compatibility_preview_intent
    admitted = positive_intent
    if admitted:
        reason = (
            "A bounded dry-run preview established a compatible provider, owner, "
            "operation, and permission shape; live provider authority remains absent."
            if compatibility_preview_intent and not canonical_positive_intent
            else (
                "Canonical semantic planning established the matching provider, owner, "
                "intent, operation, and permission boundary. Exact fields and provider "
                "receipts remain deterministic."
            )
        )
    elif plan is None:
        reason = (
            "No semantic plan established provider work; continue through shared "
            "interpretation without a provider-field blocker."
        )
    else:
        reason = (
            "The canonical plan did not establish a matching provider, owner, intent, "
            "operation, and permission boundary; request wording alone cannot grant "
            "provider execution."
        )
    return ExecutionAdmission(
        mode="provider_action" if admitted else "semantic_planning",
        provider=clean_provider,
        intent_source=(
            "compatibility_dry_run_preview"
            if compatibility_preview_intent and not canonical_positive_intent
            else "semantic_plan"
            if plan is not None
            else ""
        ),
        positive_intent=positive_intent,
        provider_action_bound=bool(provider_action_bound),
        can_execute_provider_action=admitted,
        can_block_for_missing_fields=admitted,
        reason=reason,
    )


__all__ = [
    "AdmissionMode",
    "ExecutionAdmission",
    "admit_provider_action",
]
