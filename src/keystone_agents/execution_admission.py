"""Shared admission contract for natural-language provider actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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

    plan_intent = str(getattr(semantic_plan, "intent", "") or "").strip()
    plan_agent = str(getattr(semantic_plan, "target_agent", "") or "").strip()
    plan_provider = str(
        getattr(semantic_plan, "provider_system", "") or ""
    ).strip()
    positive_intent = bool(
        semantic_plan is not None
        and plan_provider == clean_provider
        and plan_intent in allowed_intents
        and plan_agent in allowed_agents
    )
    admitted = positive_intent
    if admitted:
        reason = (
            "Shared semantic planning established the matching provider, owner, "
            "and intent. Exact fields and provider receipts remain deterministic."
        )
    elif semantic_plan is None:
        reason = (
            "No semantic plan established provider work; continue through shared "
            "interpretation without a provider-field blocker."
        )
    else:
        reason = (
            "The semantic plan did not establish the matching provider, owner, "
            "and intent; request wording alone cannot grant provider execution."
        )
    return ExecutionAdmission(
        mode="provider_action" if admitted else "semantic_planning",
        provider=clean_provider,
        intent_source="semantic_plan" if semantic_plan is not None else "",
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
