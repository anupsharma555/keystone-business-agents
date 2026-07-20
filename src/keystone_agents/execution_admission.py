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

    Provider words and action verbs are evidence, not authority. Natural
    language reaches a provider action only when the shared semantic plan and a
    bounded provider-action detector agree. Exact typed controls may bypass
    semantic planning because their operation and target were established by
    the control contract rather than inferred from prose.
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
    """Require two independent signals before natural-language provider work.

    ``provider_action_bound`` comes from a narrow deterministic detector that
    proves an operation is attached to the provider object. ``semantic_plan``
    comes from the shared interpretation layer. A provider handler may request
    missing action fields only after both agree.
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
    positive_intent = bool(
        semantic_plan is not None
        and plan_intent in allowed_intents
        and plan_agent in allowed_agents
    )
    admitted = bool(provider_action_bound and positive_intent)
    if admitted:
        reason = (
            "Shared semantic planning established a provider-write intent and "
            "the bounded detector established an action tied to this provider."
        )
    elif not provider_action_bound:
        reason = (
            "No provider-bound operation was established; continue through shared "
            "semantic execution without a provider-field blocker."
        )
    else:
        reason = (
            "Provider-action wording alone is not execution authority; the shared "
            "semantic plan did not establish the matching provider-write intent."
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
