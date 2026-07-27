"""Derived execution contract compiled from the canonical semantic plan."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from keystone_agents.schemas.manual_request_plan import (
    ManualProviderActionStep,
    ManualRequestPlan,
)
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints

TargetSetMode = Literal[
    "unspecified",
    "fixed_single",
    "fixed_set",
    "anchor_discovery",
    "open_discovery",
]


class RequestTargetScope(BaseModel):
    """Typed entity topology for one request."""

    model_config = ConfigDict(frozen=True)

    mode: TargetSetMode = "unspecified"
    target_type: str = "unknown"
    primary_target: str = ""
    anchor_entity: str = ""
    anchor_source: Literal["none", "explicit", "legacy_single_entity"] = "none"
    required_entities: tuple[str, ...] = ()
    fixed_targets: tuple[str, ...] = ()
    discovery_required: bool = False


class RequestEvidencePolicy(BaseModel):
    """Evidence expectations that survive routing and handoffs."""

    model_config = ConfigDict(frozen=True)

    depth: str = "unspecified"
    source_type_preferences: tuple[str, ...] = ()
    strict_filter_mode: str = "unspecified"
    required_dimensions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()


class RequestOutputContract(BaseModel):
    """Reader-facing artifact and formatting expectations."""

    model_config = ConfigDict(frozen=True)

    artifact_type: str = "none"
    output_form: str = "unspecified"
    audience_scope: str = "unspecified"
    output_constraints: InterpretedOutputConstraints


class RequestCapabilityPolicy(BaseModel):
    """Requested provider access without granting runtime authority."""

    model_config = ConfigDict(frozen=True)

    provider_system: str = "unspecified"
    provider_operations: tuple[str, ...] = ()
    provider_action_steps: tuple[ManualProviderActionStep, ...] = ()
    live_search_requested: bool = False
    permission_state: str = "unspecified"
    side_effect_policy: str = "draft_or_read_only"


class RequestCompletionPolicy(BaseModel):
    """Typed completion semantics independent of provider implementation."""

    model_config = ConfigDict(frozen=True)

    desired_count: int | None = None
    desired_count_mode: str = "unspecified"
    desired_count_scope: str = "unspecified"
    stop_condition: str = ""


class RequestExecutionContract(BaseModel):
    """Immutable derived view used by execution services.

    ``ManualRequestPlan`` remains the public semantic-plan and wire-format
    authority. This contract only groups its orthogonal fields so downstream
    execution does not reinterpret the operator's prose.
    """

    model_config = ConfigDict(frozen=True)

    schema_name: str = "keystone.request_execution_contract.v1"
    raw_request: str = ""
    requested_agent: str | None = None
    owner_agent: str = "clarification"
    workflow: tuple[str, ...] = ()
    intent: str = "clarification"
    objective: str = ""
    task_objective: str = "clarification"
    target: RequestTargetScope
    evidence: RequestEvidencePolicy
    output: RequestOutputContract
    capabilities: RequestCapabilityPolicy
    completion: RequestCompletionPolicy


def _compile_request_execution_contract(
    plan: ManualRequestPlan,
    *,
    raw_request: str = "",
) -> RequestExecutionContract:
    """Compile one execution view without reparsing natural-language text."""

    required_entities = tuple(dict.fromkeys(plan.required_entities))
    discovery_required = bool(plan.requires_target_discovery)
    anchor_entity = plan.anchor_entity
    anchor_source: Literal["none", "explicit", "legacy_single_entity"] = (
        "explicit" if anchor_entity and discovery_required else "none"
    )
    if discovery_required and not anchor_entity and len(required_entities) == 1:
        anchor_entity = required_entities[0]
        anchor_source = "legacy_single_entity"

    fixed_targets: tuple[str, ...] = ()
    if not discovery_required and len(required_entities) > 1:
        mode: TargetSetMode = "fixed_set"
        fixed_targets = required_entities
    elif discovery_required and anchor_entity:
        mode = "anchor_discovery"
    elif discovery_required:
        mode = "open_discovery"
    elif required_entities or plan.primary_target:
        mode = "fixed_single"
    else:
        mode = "unspecified"

    desired_count = plan.desired_count if plan.desired_count_explicit else None
    desired_count_mode = plan.desired_count_mode if plan.desired_count_explicit else "unspecified"
    if desired_count is not None and desired_count_mode == "unspecified":
        desired_count_mode = "target"
    desired_count_scope = plan.desired_count_scope if plan.desired_count_explicit else "unspecified"

    return RequestExecutionContract(
        raw_request=str(raw_request or "").strip(),
        requested_agent=plan.requested_agent,
        owner_agent=plan.target_agent,
        workflow=tuple(plan.workflow),
        intent=plan.intent,
        objective=plan.objective,
        task_objective=plan.task_objective,
        target=RequestTargetScope(
            mode=mode,
            target_type=plan.target_type,
            primary_target=plan.primary_target,
            anchor_entity=anchor_entity,
            anchor_source=anchor_source,
            required_entities=required_entities,
            fixed_targets=fixed_targets,
            discovery_required=discovery_required,
        ),
        evidence=RequestEvidencePolicy(
            depth=plan.ask_shape.evidence_depth,
            source_type_preferences=tuple(plan.ask_shape.source_type_preference),
            strict_filter_mode=plan.ask_shape.strict_filter_mode,
            required_dimensions=tuple(plan.required_terms),
            constraints=tuple(plan.constraints),
        ),
        output=RequestOutputContract(
            artifact_type=plan.expected_artifact_type,
            output_form=plan.ask_shape.output_form,
            audience_scope=plan.ask_shape.audience_scope,
            output_constraints=plan.ask_shape.output_constraints.model_copy(deep=True),
        ),
        capabilities=RequestCapabilityPolicy(
            provider_system=plan.provider_system,
            provider_operations=tuple(plan.provider_operations),
            provider_action_steps=tuple(
                step.model_copy(deep=True) for step in plan.provider_action_steps
            ),
            live_search_requested=plan.requires_live_search,
            permission_state=plan.ask_shape.permission_state,
            side_effect_policy=plan.side_effect_policy,
        ),
        completion=RequestCompletionPolicy(
            desired_count=desired_count,
            desired_count_mode=desired_count_mode,
            desired_count_scope=desired_count_scope,
            stop_condition=plan.ask_shape.stop_condition,
        ),
    )


__all__ = [
    "RequestCapabilityPolicy",
    "RequestCompletionPolicy",
    "RequestEvidencePolicy",
    "RequestExecutionContract",
    "RequestOutputContract",
    "RequestTargetScope",
    "TargetSetMode",
]
