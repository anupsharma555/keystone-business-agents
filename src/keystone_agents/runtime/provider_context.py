"""Validation and handoff contracts for read-only cross-provider context stages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from keystone_agents.runtime.tool_execution import sdk_tool_output_payloads
from keystone_agents.schemas.decision_ownership import (
    AgentDecisionRecord,
    DecisionValidatorOutcome,
)
from keystone_agents.schemas.manual_request_plan import (
    ManualRequestPlan,
)


class ProviderContextStageResult(BaseModel):
    """One agent-selected, provider-verified read context for a later specialist."""

    model_config = ConfigDict(extra="forbid")

    schema_name: str = "keystone.provider_context_stage.v1"
    provider_system: str
    resource_type: str
    selected_object: dict[str, Any] = Field(default_factory=dict)
    decision: AgentDecisionRecord
    validator_outcome: DecisionValidatorOutcome
    tool_names: list[str] = Field(default_factory=list)
    provider_receipts: list[dict[str, Any]] = Field(default_factory=list)
    manager_agent: str = ""
    downstream_agent: str = ""
    handoff_confirmed: bool = False
    read_only: bool = True


def validate_calendar_provider_context_decision(
    raw_result: Any,
    decisions: Sequence[AgentDecisionRecord],
    *,
    manager_output: Any | None = None,
    required_downstream_agent: str = "",
) -> ProviderContextStageResult:
    """Bind a Chief/Orchestrator Calendar choice to actual tool-returned events."""

    outputs = sdk_tool_output_payloads(
        raw_result,
        tool_name="read_google_calendar_window",
    )
    events: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for entry in outputs:
        output = entry.get("output") or {}
        if isinstance(output, Mapping):
            receipts.append(_bounded_calendar_receipt(output))
            for event in output.get("events") or []:
                if isinstance(event, Mapping) and str(event.get("event_id") or "").strip():
                    events.append(dict(event))
    calendar_decision = next(
        (
            decision
            for decision in decisions
            if decision.decision_stage
            in {"calendar_context_selection", "google_calendar_context_selection"}
        ),
        None,
    )
    if calendar_decision is None:
        calendar_decision = AgentDecisionRecord(
            decision_owner="chief_of_staff",
            decision_stage="calendar_context_selection",
            limitations=["No typed Calendar context decision was returned."],
            needs_more_context=True,
        )
    selected_id = str(calendar_decision.selected_candidate_id or "").strip()
    candidate_ids = {
        str(event.get("event_id") or "").strip() for event in events
    }
    assessments = {
        item.candidate_id: item.disposition
        for item in calendar_decision.candidate_assessments
    }
    selected = next(
        (event for event in events if str(event.get("event_id") or "").strip() == selected_id),
        None,
    )
    downstream_agent = str(required_downstream_agent or "").strip()
    manager_decision = getattr(manager_output, "decision", None)
    durable_handoff = getattr(manager_output, "durable_handoff", None)
    durable_handoff_agent = str(getattr(durable_handoff, "agent", "") or "").strip()
    manager_selected_ids = (
        set(manager_decision.selected_candidate_ids)
        if isinstance(manager_decision, AgentDecisionRecord)
        else set()
    )
    handoff_confirmed = bool(
        downstream_agent
        and durable_handoff_agent == downstream_agent
        and downstream_agent in manager_selected_ids
    )
    if downstream_agent and durable_handoff_agent != downstream_agent:
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            reason_code="required_manager_handoff_missing",
            feedback=(
                "The manager must explicitly return a durable handoff to "
                f"{downstream_agent} before that specialist may receive Calendar context."
            ),
        )
    elif downstream_agent and downstream_agent not in manager_selected_ids:
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            reason_code="manager_handoff_decision_not_selected",
            feedback=(
                "The manager's typed decision must explicitly select the same downstream "
                f"owner named by its durable handoff: {downstream_agent}."
            ),
        )
    elif calendar_decision.decision_owner not in {"chief_of_staff", "orchestrator"}:
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            reason_code="invalid_calendar_context_decision_owner",
            feedback=(
                "Calendar cross-provider context must be selected by Chief of Staff "
                "or Orchestrator, not by a deterministic helper or unrelated specialist."
            ),
        )
    elif calendar_decision.needs_more_context and not selected_id:
        validator = DecisionValidatorOutcome(
            status="accepted",
            decision_stage="calendar_context_selection",
            candidate_count=len(events),
            reason_code="agent_requested_more_calendar_context",
            feedback="The manager agent declined to guess among Calendar candidates.",
        )
    elif selected is None:
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=False,
            reason_code=(
                "missing_selected_calendar_event"
                if not selected_id
                else "fabricated_selected_calendar_event"
            ),
            feedback=(
                "Select one exact event_id returned by read_google_calendar_window, "
                "or explicitly request more context."
            ),
        )
    elif assessments.get(selected_id) != "selected":
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=True,
            reason_code="calendar_selected_assessment_missing",
            feedback=(
                "Mark the selected event as selected and assess every returned alternative."
            ),
        )
    elif len(events) > 1 and not candidate_ids.issubset(assessments):
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=True,
            reason_code="calendar_candidate_assessments_incomplete",
            feedback="Assess every returned Calendar candidate before handing one to Gmail.",
        )
    elif any(
        assessments.get(candidate_id) != "excluded"
        for candidate_id in candidate_ids
        if candidate_id != selected_id
    ):
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=True,
            reason_code="calendar_alternatives_not_excluded",
            feedback=(
                "Explain why each non-selected Calendar event does not match the request."
            ),
        )
    elif not str(calendar_decision.reasoning or "").strip():
        validator = DecisionValidatorOutcome(
            status="repair_required",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=True,
            reason_code="calendar_selection_reasoning_missing",
            feedback="Explain why the selected event best matches the operator request.",
        )
    else:
        validator = DecisionValidatorOutcome(
            status="accepted",
            decision_stage="calendar_context_selection",
            selected_candidate_id=selected_id,
            candidate_count=len(events),
            selected_identity_in_candidate_set=True,
            selected_identity_was_read=True,
            reason_code="manager_selection_bound_to_calendar_result_set",
            feedback="The selected event was returned by the bounded Calendar read.",
        )
    return ProviderContextStageResult(
        provider_system="google_calendar",
        resource_type="calendar_event",
        selected_object=(
            _bounded_calendar_context(selected or {})
            if validator.status == "accepted"
            else {}
        ),
        decision=calendar_decision,
        validator_outcome=validator,
        tool_names=["read_google_calendar_window"] if outputs else [],
        provider_receipts=receipts,
        manager_agent=(
            str(getattr(manager_output, "agent_name", "") or "").strip()
            if manager_output is not None
            else ""
        ),
        downstream_agent=downstream_agent,
        handoff_confirmed=handoff_confirmed,
    )


def provider_context_requirements_satisfied(
    plan: ManualRequestPlan,
    stages: Sequence[ProviderContextStageResult],
) -> tuple[bool, list[str]]:
    """Validate all required read-only stages without expanding action authority."""

    required = [item for item in plan.provider_context_requirements if item.required]
    missing: list[str] = []
    for requirement in required:
        stage = next(
            (
                item
                for item in stages
                if item.provider_system == requirement.provider_system
                and item.resource_type == requirement.resource_type
                and item.read_only
                and item.validator_outcome.status == "accepted"
                and bool(item.selected_object)
            ),
            None,
        )
        if stage is None:
            missing.append(f"{requirement.provider_system}:{requirement.resource_type}")
    return not missing, missing


def provider_context_handoff_text(stages: Sequence[ProviderContextStageResult]) -> str:
    """Render bounded verified context for the primary specialist prompt."""

    payloads = [
        {
            "provider_system": stage.provider_system,
            "resource_type": stage.resource_type,
            "selected_object": stage.selected_object,
            "decision": stage.decision.model_dump(mode="json"),
            "validator_outcome": stage.validator_outcome.model_dump(mode="json"),
            "manager_agent": stage.manager_agent,
            "downstream_agent": stage.downstream_agent,
            "handoff_confirmed": stage.handoff_confirmed,
        }
        for stage in stages
        if stage.validator_outcome.status == "accepted" and stage.selected_object
    ]
    if not payloads:
        return ""
    import json

    return (
        "Verified read-only provider context selected by the manager agent. "
        "Use it as evidence, not as mutation authority:\n"
        + json.dumps(payloads, ensure_ascii=True, sort_keys=True)
    )


def _bounded_calendar_context(event: Mapping[str, Any]) -> dict[str, Any]:
    allowed = (
        "event_id",
        "title",
        "display_start_date",
        "display_start_time",
        "display_end_date",
        "display_end_time",
        "timezone",
        "organizer",
        "attendees",
        "description",
        "source_calendar_id",
    )
    output: dict[str, Any] = {}
    for key in allowed:
        value = event.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            output[key] = [str(item)[:320] for item in value[:20]]
        else:
            output[key] = str(value)[:4000 if key == "description" else 500]
    return output


def _bounded_calendar_receipt(output: Mapping[str, Any]) -> dict[str, Any]:
    """Keep provider proof without copying all returned event content into handoffs."""

    verification = output.get("verification")
    verification_mapping = verification if isinstance(verification, Mapping) else {}
    events = output.get("events")
    return {
        "status": str(output.get("status") or "")[:80],
        "provider": str(output.get("provider") or "google_calendar")[:120],
        "candidate_count": len(events) if isinstance(events, list | tuple) else 0,
        "verification": {
            "passed": verification_mapping.get("passed") is True,
            "reason_code": str(verification_mapping.get("reason_code") or "")[:160],
        },
        "receipt_id": str(output.get("receipt_id") or "")[:200],
    }


__all__ = [
    "ProviderContextStageResult",
    "provider_context_handoff_text",
    "provider_context_requirements_satisfied",
    "validate_calendar_provider_context_decision",
]
