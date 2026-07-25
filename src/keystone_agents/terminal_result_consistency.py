"""Keep final review truth aligned with the reader-facing result."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.schemas.execution_request import ExecutionPublicResult

_FAILED_REVIEW_STATUSES = {"fail", "failed", "failure"}
_TERMINAL_NON_SUCCESS_STATES = {
    "blocked",
    "canceled",
    "failed",
    "needs_input",
    "partial",
}


def reconcile_failed_review(
    payload: Mapping[str, Any],
    result: ExecutionPublicResult,
) -> ExecutionPublicResult:
    """Prevent a failed final review from publishing a success result.

    A verified provider mutation cannot be undone by a later rendering failure,
    so that case is partial. Read-only work with no durable provider effect is a
    failed result. Review diagnostics stay in structured metadata rather than
    being copied into the reader-facing text.
    """

    if result.status in _TERMINAL_NON_SUCCESS_STATES:
        return result
    if not _current_review_failed(payload):
        return result

    provider_mutation_verified = bool(
        result.provider_write_attempted and result.provider_receipt_verified is True
    )
    if provider_mutation_verified:
        return result.model_copy(
            update={
                "status": "partial",
                "title": "Business Agents Partially Completed",
                "omit_title": False,
                "text": (
                    "The provider action completed and was verified, but the "
                    "reader-facing result did not pass final review. Review the "
                    "provider state before retrying."
                ),
                "completion_confirmed": False,
                "failure_code": "orchestrator_review_failed",
                "failure_summary": (
                    "Provider action verified; reader-facing result failed final review."
                ),
            }
        )
    return result.model_copy(
        update={
            "status": "failed",
            "title": "Business Agents Run Failed",
            "omit_title": False,
            "text": "Business Agents could not verify a reader-ready result.",
            "completion_confirmed": False,
            "failure_code": "orchestrator_review_failed",
            "failure_summary": "Reader-facing result failed final review.",
        }
    )


def _current_review_failed(payload: Mapping[str, Any]) -> bool:
    review = _current_review(payload)
    if not review:
        return False
    statuses = (
        review.get("status"),
        review.get("review_status"),
        review.get("deterministic_review_status"),
        review.get("hybrid_final_status"),
    )
    checks = review.get("test_pack_checks")
    if isinstance(checks, Mapping):
        statuses = (
            *statuses,
            checks.get("deterministic_review_status"),
            checks.get("hybrid_final_status"),
        )
    baseline = review.get("deterministic_baseline")
    if isinstance(baseline, Mapping):
        statuses = (*statuses, baseline.get("status"))
    return any(
        str(status or "").strip().lower() in _FAILED_REVIEW_STATUSES
        for status in statuses
    )


def _current_review(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for container in _review_containers(payload):
        direct = container.get("orchestrator_review")
        if isinstance(direct, Mapping):
            return direct
        reviews = container.get("orchestrator_reviews")
        if isinstance(reviews, list):
            current = next(
                (item for item in reversed(reviews) if isinstance(item, Mapping)),
                None,
            )
            if current is not None:
                return current
    return {}


def _review_containers(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    containers: list[Mapping[str, Any]] = [payload]
    for key in ("output", "script_payload", "work_item"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
            nested_output = value.get("output")
            if isinstance(nested_output, Mapping):
                containers.append(nested_output)
            target = value.get("target")
            if isinstance(target, Mapping):
                metadata = target.get("metadata")
                if isinstance(metadata, Mapping):
                    containers.append(metadata)
    return tuple(containers)


__all__ = ["reconcile_failed_review"]
