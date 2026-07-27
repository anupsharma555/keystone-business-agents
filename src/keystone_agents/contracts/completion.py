"""Pure request-completion contracts shared below workflow orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from keystone_agents.schemas.request_coverage import RequestCoverage

DETERMINISTIC_COVERAGE_ENFORCEMENT = "deterministic"
_HARD_COVERAGE_STATUSES = frozenset({"blocked"})
_HARD_OUTPUT_FORM_STATUSES = frozenset({"unmet"})
_HARD_STOP_CONDITION_STATUSES = frozenset({"blocked", "violated"})


@dataclass(frozen=True)
class BoundedSearchReceipt:
    """Host-owned proof that one finite provider search was fully processed."""

    provider_attempt_count: int
    planned_attempt_count: int
    provider_completed: bool
    budget_or_deadline_stopped: bool
    discovered_candidate_count: int
    processed_candidate_count: int
    query_attempt_count: int | None = None
    query_completed_count: int | None = None
    query_uncovered_count: int | None = None
    query_ledger_valid: bool | None = None

    @property
    def exhausted(self) -> bool:
        attempted = max(0, int(self.provider_attempt_count))
        planned = max(0, int(self.planned_attempt_count))
        discovered = max(0, int(self.discovered_candidate_count))
        processed = max(0, int(self.processed_candidate_count))
        if self.query_ledger_valid is not None:
            query_attempted = max(0, int(self.query_attempt_count or 0))
            query_completed = max(0, int(self.query_completed_count or 0))
            query_uncovered = max(0, int(self.query_uncovered_count or 0))
            search_completed = bool(
                self.query_ledger_valid
                and query_attempted > 0
                and query_attempted >= planned
                and query_completed >= planned
                and query_uncovered == 0
            )
        else:
            search_completed = bool(
                attempted > 0
                and attempted >= planned
                and self.provider_completed
            )
        return bool(
            search_completed
            and not self.budget_or_deadline_stopped
            and processed >= discovered
        )

    def receipt(self) -> dict[str, int | bool | None]:
        payload: dict[str, int | bool | None] = {
            "provider_attempt_count": max(0, int(self.provider_attempt_count)),
            "planned_attempt_count": max(0, int(self.planned_attempt_count)),
            "provider_completed": bool(self.provider_completed),
            "budget_or_deadline_stopped": bool(self.budget_or_deadline_stopped),
            "discovered_candidate_count": max(
                0, int(self.discovered_candidate_count)
            ),
            "processed_candidate_count": max(
                0, int(self.processed_candidate_count)
            ),
            "exhausted": self.exhausted,
        }
        if self.query_ledger_valid is not None:
            payload.update(
                {
                    "query_attempt_count": max(
                        0,
                        int(self.query_attempt_count or 0),
                    ),
                    "query_completed_count": max(
                        0,
                        int(self.query_completed_count or 0),
                    ),
                    "query_uncovered_count": max(
                        0,
                        int(self.query_uncovered_count or 0),
                    ),
                    "query_ledger_valid": bool(self.query_ledger_valid),
                }
            )
        return payload


@dataclass(frozen=True)
class CompletionDecision:
    """Pure decision about whether deterministic coverage permits success."""

    completion_allowed: bool
    reason_code: str = ""
    unmet_dimensions: tuple[str, ...] = ()
    next_safe_action: str = ""
    blocking_count: int = 0


def evaluate_deterministic_completion(
    rows: Iterable[RequestCoverage],
) -> CompletionDecision:
    """Evaluate host-owned coverage once for every entrypoint adapter."""

    blocked = blocking_request_coverage(rows)
    if not blocked:
        return CompletionDecision(completion_allowed=True)
    unmet_dimensions = tuple(
        dict.fromkeys(
            dimension.strip()
            for coverage in blocked
            for dimension in coverage.unmet_dimensions
            if dimension.strip()
        )
    )
    if not unmet_dimensions:
        unmet_dimensions = (
            "The completed artifact did not satisfy the requested output contract.",
        )
    next_safe_action = next(
        (
            coverage.next_safe_action.strip()
            for coverage in blocked
            if coverage.next_safe_action.strip()
        ),
        "Complete the unmet request dimensions before claiming success.",
    )
    return CompletionDecision(
        completion_allowed=False,
        reason_code="request_contract_incomplete",
        unmet_dimensions=unmet_dimensions,
        next_safe_action=next_safe_action,
        blocking_count=len(blocked),
    )


def bounded_search_receipt_from_provider_telemetry(
    telemetry: Mapping[str, Any],
    *,
    planned_attempt_count: int,
    discovered_candidate_count: int,
    processed_candidate_count: int,
    budget_or_deadline_stopped: bool = False,
) -> BoundedSearchReceipt:
    """Compile conservative search completion proof from provider-owned counters.

    Query coverage is authoritative when available. Provider-call totals are a
    compatibility fallback because one planned query may legitimately fan out
    across optional providers or encounter an optional-lane cap after another
    provider has already completed the query.
    """

    usage = telemetry.get("provider_usage")
    usage_rows = usage.values() if isinstance(usage, Mapping) else ()
    attempted = 0
    succeeded = 0
    for row in usage_rows:
        if not isinstance(row, Mapping):
            continue
        attempted += max(0, int(row.get("requests_attempted") or 0))
        succeeded += max(0, int(row.get("requests_succeeded") or 0))
    errors = telemetry.get("search_provider_errors")
    error_rows = list(errors) if isinstance(errors, (list, tuple)) else []
    planned = max(0, int(planned_attempt_count))
    query_keys = {
        "query_attempt_count",
        "query_completed_count",
        "query_uncovered_count",
    }
    present_query_keys = query_keys.intersection(telemetry)
    query_ledger_valid: bool | None = None
    query_attempted: int | None = None
    query_completed: int | None = None
    query_uncovered: int | None = None
    if present_query_keys:
        try:
            query_attempted = int(telemetry.get("query_attempt_count") or 0)
            query_completed = int(telemetry.get("query_completed_count") or 0)
            query_uncovered = int(telemetry.get("query_uncovered_count") or 0)
        except (TypeError, ValueError):
            query_attempted = query_completed = query_uncovered = 0
        provider_evidence_present = isinstance(usage, Mapping) and any(
            isinstance(row, Mapping) for row in usage.values()
        )
        query_ledger_valid = bool(
            present_query_keys == query_keys
            and query_attempted >= 0
            and 0 <= query_completed <= query_attempted
            and query_uncovered == query_attempted - query_completed
            and (
                not provider_evidence_present
                or succeeded >= query_completed
            )
        )
        stopped = bool(
            budget_or_deadline_stopped
            or not query_ledger_valid
            or query_uncovered > 0
            or query_attempted < planned
        )
    else:
        stopped = bool(budget_or_deadline_stopped or error_rows)
    return BoundedSearchReceipt(
        provider_attempt_count=attempted,
        planned_attempt_count=planned,
        provider_completed=bool(
            planned > 0
            and succeeded >= planned
            and attempted >= planned
            and not budget_or_deadline_stopped
            and not error_rows
        ),
        budget_or_deadline_stopped=stopped,
        discovered_candidate_count=discovered_candidate_count,
        processed_candidate_count=processed_candidate_count,
        query_attempt_count=query_attempted,
        query_completed_count=query_completed,
        query_uncovered_count=query_uncovered,
        query_ledger_valid=query_ledger_valid,
    )


def deterministic_request_coverage(
    container: Mapping[str, object],
) -> RequestCoverage | None:
    """Return only coverage explicitly compiled by the host execution layer."""

    if (
        container.get("request_coverage_enforcement")
        != DETERMINISTIC_COVERAGE_ENFORCEMENT
    ):
        return None
    value = container.get("request_coverage")
    if not isinstance(value, Mapping):
        return None
    try:
        coverage = RequestCoverage.model_validate(value)
    except ValueError:
        return None
    return coverage if coverage.status != "unassessed" else None


def blocking_request_coverage(
    rows: Iterable[RequestCoverage],
) -> list[RequestCoverage]:
    """Return host-owned coverage rows that prohibit a success claim."""

    return [
        row
        for row in rows
        if row.status in _HARD_COVERAGE_STATUSES
        or row.output_form_status in _HARD_OUTPUT_FORM_STATUSES
        or row.stop_condition_status in _HARD_STOP_CONDITION_STATUSES
    ]


def build_count_request_coverage(
    *,
    interpreted_request: str,
    expected_count: int,
    observed_count: int,
    item_label: str,
    next_safe_action: str,
    count_mode: str = "target",
    bounded_search_exhausted: bool = False,
) -> RequestCoverage:
    """Build a deterministic count contract without interpreting request prose."""

    expected = max(0, int(expected_count or 0))
    observed = max(0, int(observed_count or 0))
    if expected <= 0:
        return RequestCoverage(interpreted_request=interpreted_request)
    mode = str(count_mode or "target").strip().lower()
    exceeded_maximum = mode == "maximum" and observed > expected
    complete = (
        (observed == expected or (observed < expected and bounded_search_exhausted))
        if mode == "maximum"
        else observed == expected
        if mode == "exact"
        else observed >= expected
    )
    if complete:
        capped_target_note = (
            (
                f"returned {observed} {item_label} after the bounded search "
                f"exhausted qualified results below the cap of {expected}"
            )
            if mode == "maximum" and observed < expected
            else f"returned {observed} {item_label} for the requested count of {expected}"
        )
        return RequestCoverage(
            interpreted_request=interpreted_request,
            status="complete",
            satisfied_dimensions=[capped_target_note],
            output_form_status="satisfied",
            stop_condition_status="satisfied",
        )
    return RequestCoverage(
        interpreted_request=interpreted_request,
        status="partial",
        satisfied_dimensions=[f"returned {observed} source-backed {item_label}"],
        unmet_dimensions=[
            (
                f"requested at most {expected} {item_label} but {observed} were returned"
                if exceeded_maximum
                else (
                    f"targeted up to {expected} {item_label} but only {observed} "
                    "were returned without bounded-search exhaustion evidence"
                )
                if mode == "maximum"
                else (
                    f"requested exactly {expected} {item_label} but {observed} "
                    "met the current gates"
                )
                if mode == "exact"
                else (
                    f"requested {expected} {item_label} but only {observed} "
                    "met the current gates"
                )
            )
        ],
        output_form_status="satisfied",
        stop_condition_status=("violated" if exceeded_maximum else "blocked"),
        next_safe_action=next_safe_action,
    )


__all__ = [
    "BoundedSearchReceipt",
    "DETERMINISTIC_COVERAGE_ENFORCEMENT",
    "blocking_request_coverage",
    "bounded_search_receipt_from_provider_telemetry",
    "build_count_request_coverage",
    "deterministic_request_coverage",
]
