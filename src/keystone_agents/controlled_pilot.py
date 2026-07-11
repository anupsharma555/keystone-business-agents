"""Executable ANU-61 controlled-pilot catalog and receipt validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TRUSTED_RUNTIME_PREREQUISITES: tuple[str, ...] = (
    "L174-14",
    "L174-16",
    "L174-19",
    "L174-20",
)


@dataclass(frozen=True)
class ControlledPilotCase:
    """One representative natural ask promoted into the controlled pilot."""

    case_id: str
    title: str
    natural_ask: str
    expected_entry_owner: str
    backend: Literal["direct_specialist", "langgraph"]
    context_sources: tuple[str, ...]
    expected_artifact: str
    allowed_provider_writes: int
    requires_cleanup: bool
    max_openai_requests: int
    max_cost_usd: float
    differentiation_case_ids: tuple[str, ...]


@dataclass(frozen=True)
class ControlledPilotObservation:
    """Sanitized backend and Slack evidence from one pilot execution."""

    case_id: str
    natural_request_sha256: str
    slack_permalink_present: bool
    answer_first: bool
    final_response_count: int
    route_correct: bool
    graph_used: bool
    work_item_continuity: bool
    visible_source_count: int
    manual_provider_ids: int
    approval_round_trips: int
    provider_writes: int
    provider_readback_verified: bool
    cleanup_verified: bool
    unintended_writes: int
    duplicate_artifacts: int
    developer_intervention: bool
    openai_requests: int
    estimated_cost_usd: float
    latency_ms: int
    trace_or_run_ref_present: bool


@dataclass(frozen=True)
class ControlledPilotAssessment:
    case_id: str
    status: Literal["pass", "fail"]
    failed_checks: tuple[str, ...]


CONTROLLED_PILOT_CASES: tuple[ControlledPilotCase, ...] = (
    ControlledPilotCase(
        case_id="research_to_internal_doc",
        title="Source-backed research to verified internal Doc",
        natural_ask=(
            "Research the selected company for current KNI relevance, then create one "
            "concise internal Google Doc from the approved source-backed result and return "
            "the verified Doc link."
        ),
        expected_entry_owner="chief_of_staff",
        backend="langgraph",
        context_sources=("public_research", "google_workspace"),
        expected_artifact="verified_google_doc",
        allowed_provider_writes=1,
        requires_cleanup=False,
        max_openai_requests=4,
        max_cost_usd=0.15,
        differentiation_case_ids=(
            "source_backed_specialists",
            "durable_approvals_audit",
            "operational_follow_through",
        ),
    ),
    ControlledPilotCase(
        case_id="selected_gmail_thread_followup",
        title="Selected Gmail thread to current-state next step",
        natural_ask=(
            "Review the selected Gmail thread including all messages, identify the current "
            "conversation state, and recommend the most useful KNI-specific next step. "
            "Include reply copy only if replying now would move the relationship forward."
        ),
        expected_entry_owner="chief_of_staff",
        backend="langgraph",
        context_sources=("gmail_selected_thread", "approved_kni_context"),
        expected_artifact="reviewed_next_action_or_draft",
        allowed_provider_writes=0,
        requires_cleanup=False,
        max_openai_requests=6,
        max_cost_usd=0.25,
        differentiation_case_ids=(
            "slack_native_execution",
            "persistent_workitem_state",
            "typed_context_handoffs",
        ),
    ),
    ControlledPilotCase(
        case_id="current_opportunity_assessment",
        title="Current source-backed opportunity assessment",
        natural_ask=(
            "Assess the selected current opportunity for KNI fit, separate confirmed facts "
            "from interpretation, show the retained sources, and recommend the next safe action."
        ),
        expected_entry_owner="opportunity_scout",
        backend="direct_specialist",
        context_sources=("selected_opportunity_sources",),
        expected_artifact="opportunity_assessment",
        allowed_provider_writes=0,
        requires_cleanup=False,
        max_openai_requests=2,
        max_cost_usd=0.10,
        differentiation_case_ids=(
            "source_backed_specialists",
            "operational_follow_through",
        ),
    ),
    ControlledPilotCase(
        case_id="weekly_project_brief",
        title="Broad weekly or project brief",
        natural_ask=(
            "Prepare a concise weekly Keystone project brief from the approved bounded "
            "sources, covering decisions, completed work, blockers, and next actions, then "
            "return one answer-first Slack receipt."
        ),
        expected_entry_owner="chief_of_staff",
        backend="langgraph",
        context_sources=("slack", "gmail", "completed_work_items", "calendar"),
        expected_artifact="weekly_project_brief",
        allowed_provider_writes=0,
        requires_cleanup=False,
        max_openai_requests=3,
        max_cost_usd=0.15,
        differentiation_case_ids=(
            "slack_native_execution",
            "persistent_workitem_state",
            "operational_follow_through",
        ),
    ),
)


def controlled_pilot_cases() -> tuple[ControlledPilotCase, ...]:
    return CONTROLLED_PILOT_CASES


def controlled_pilot_ready(trusted_runtime_rows: dict[str, str]) -> bool:
    """Require all four trusted rows before starting the live pilot."""

    return all(
        str(trusted_runtime_rows.get(row) or "").casefold() == "pass"
        for row in TRUSTED_RUNTIME_PREREQUISITES
    )


def assess_controlled_pilot_observation(
    observation: ControlledPilotObservation,
) -> ControlledPilotAssessment:
    case = next(
        (item for item in CONTROLLED_PILOT_CASES if item.case_id == observation.case_id),
        None,
    )
    if case is None:
        raise ValueError(f"Unknown controlled pilot case: {observation.case_id}")
    if len(observation.natural_request_sha256) != 64:
        raise ValueError("Pilot observation requires the SHA-256 of the natural ask.")

    checks = {
        "slack_permalink_present": observation.slack_permalink_present,
        "answer_first": observation.answer_first,
        "one_final_response": observation.final_response_count == 1,
        "route_correct": observation.route_correct,
        "backend_matches": observation.graph_used == (case.backend == "langgraph"),
        "work_item_continuity": (
            case.backend == "direct_specialist" or observation.work_item_continuity
        ),
        "visible_sources": observation.visible_source_count > 0,
        "provider_ids_internal": observation.manual_provider_ids == 0,
        "approval_scope_exact": observation.approval_round_trips
        == (1 if case.allowed_provider_writes else 0),
        "write_scope_exact": observation.provider_writes == case.allowed_provider_writes,
        "readback_verified": (
            case.allowed_provider_writes == 0 or observation.provider_readback_verified
        ),
        "cleanup_verified": not case.requires_cleanup or observation.cleanup_verified,
        "no_unintended_writes": observation.unintended_writes == 0,
        "no_duplicate_artifacts": observation.duplicate_artifacts == 0,
        "no_developer_intervention": not observation.developer_intervention,
        "request_ceiling": observation.openai_requests <= case.max_openai_requests,
        "cost_ceiling": observation.estimated_cost_usd <= case.max_cost_usd,
        "latency_recorded": observation.latency_ms >= 0,
        "trace_or_run_ref_present": observation.trace_or_run_ref_present,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    return ControlledPilotAssessment(
        case_id=case.case_id,
        status="pass" if not failed else "fail",
        failed_checks=failed,
    )
