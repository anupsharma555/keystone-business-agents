"""Shared semantic-completion checks for direct and graph WorkItem paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keystone_agents.contracts.completion import (
    DETERMINISTIC_COVERAGE_ENFORCEMENT,
    blocking_request_coverage,
    build_count_request_coverage,
    evaluate_deterministic_completion,
)
from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItemBlocker,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemStatus,
)


@dataclass(frozen=True)
class _CoverageAssessment:
    """Host-owned coverage plus the artifact that established it."""

    coverage: RequestCoverage
    artifact_type: str
    source_agent: str
    artifact_id: str
    created_at: str


def _artifact_identity(artifact: Any) -> tuple[str, str, str]:
    return (
        str(getattr(artifact, "artifact_type", "") or ""),
        str(getattr(artifact, "artifact_id", "") or ""),
        str(getattr(artifact, "source_agent", "") or ""),
    )


def _deterministic_coverage_assessments(
    result: WorkflowRunResult,
) -> list[_CoverageAssessment]:
    """Read the latest host-enforced coverage for each executable stage.

    ``result.artifact_refs`` belongs to the current attempt and supersedes a
    durable artifact for the same agent/artifact boundary. This prevents an
    earlier underfilled attempt from permanently blocking a later repaired
    result while retaining coverage from other stages.
    """

    assessments_by_boundary: dict[
        tuple[str, str], tuple[tuple[int, str, int], _CoverageAssessment]
    ] = {}
    seen_artifacts: set[tuple[str, str, str]] = set()
    current_identities = {
        _artifact_identity(artifact) for artifact in result.artifact_refs
    }
    for index, artifact in enumerate(
        [*result.work_item.artifact_refs, *result.artifact_refs]
    ):
        identity = _artifact_identity(artifact)
        if identity in seen_artifacts:
            continue
        seen_artifacts.add(identity)
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        if (
            metadata.get("request_coverage_enforcement")
            != DETERMINISTIC_COVERAGE_ENFORCEMENT
        ):
            continue
        candidate: Any = metadata.get("request_coverage")
        if not isinstance(candidate, dict):
            continue
        try:
            coverage = RequestCoverage.model_validate(candidate)
        except ValueError:
            continue
        if coverage.status == "unassessed":
            continue
        assessment = _CoverageAssessment(
            coverage=coverage,
            artifact_type=str(artifact.artifact_type or ""),
            source_agent=str(artifact.source_agent or ""),
            artifact_id=str(artifact.artifact_id or ""),
            created_at=str(artifact.created_at or ""),
        )
        boundary = (assessment.source_agent, assessment.artifact_type)
        rank = (
            1 if identity in current_identities else 0,
            assessment.created_at,
            index,
        )
        existing = assessments_by_boundary.get(boundary)
        if existing is None or rank > existing[0]:
            assessments_by_boundary[boundary] = (rank, assessment)
    return [
        ranked_assessment[1]
        for ranked_assessment in assessments_by_boundary.values()
    ]


def assessed_request_coverage(result: WorkflowRunResult) -> list[RequestCoverage]:
    """Return only host-enforced, assessed coverage rows."""

    return [
        assessment.coverage
        for assessment in _deterministic_coverage_assessments(result)
    ]


def reconcile_terminal_request_completion(
    result: WorkflowRunResult,
) -> WorkflowRunResult:
    """Prevent a DONE result when assessed coverage has a hard unmet contract.

    This is intentionally conservative: advisory or unassessed coverage remains
    visible to synthesis but does not change terminal state. Only a deterministic
    blocked stop condition, violated stop condition, or unmet output form can
    convert an apparent success into a blocked result.
    """

    if result.status != WorkItemStatus.DONE:
        return result
    assessments = _deterministic_coverage_assessments(result)
    blocked_assessments = [
        assessment
        for assessment in assessments
        if blocking_request_coverage([assessment.coverage])
    ]
    if not blocked_assessments:
        return result
    decision = evaluate_deterministic_completion(
        assessment.coverage for assessment in blocked_assessments
    )
    messages = list(decision.unmet_dimensions)
    existing_codes = {blocker.code for blocker in result.work_item.blockers}
    blockers = list(result.work_item.blockers)
    if "request_contract_incomplete" not in existing_codes:
        blockers.append(
            WorkItemBlocker(
                code="request_contract_incomplete",
                message="; ".join(messages[:4]),
            )
        )
    next_safe_action = decision.next_safe_action
    repair_agent = result.route
    for assessment in blocked_assessments:
        try:
            repair_agent = WorkItemRoute(assessment.source_agent)
        except ValueError:
            continue
        break
    next_action = WorkItemNextAction(
        action="complete_request_contract",
        agent=repair_agent,
        description=next_safe_action,
        command_hint=f"keystone work-items advance {result.work_item.id}",
    )
    work_item = result.work_item.model_copy(
        update={
            "status": WorkItemStatus.BLOCKED,
            "blockers": blockers,
            "next_action": next_action,
            "audit_notes": [
                *result.work_item.audit_notes,
                "Terminal success was withheld because assessed request coverage remained blocked.",
            ],
        }
    ).touch()
    return result.model_copy(
        update={
            "work_item": work_item,
            "status": WorkItemStatus.BLOCKED,
            "blockers": [blocker for blocker in blockers if not blocker.resolved],
            "next_action": next_action,
            "audit_notes": list(
                dict.fromkeys(
                    [
                        *result.audit_notes,
                        (
                            "Terminal success was withheld because assessed request "
                            "coverage remained blocked."
                        ),
                    ]
                )
            ),
        }
    )


__all__ = [
    "assessed_request_coverage",
    "blocking_request_coverage",
    "build_count_request_coverage",
    "reconcile_terminal_request_completion",
]
