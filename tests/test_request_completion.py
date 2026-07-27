from __future__ import annotations

from keystone_agents.contracts.completion import (
    BoundedSearchReceipt,
    bounded_search_receipt_from_provider_telemetry,
    evaluate_deterministic_completion,
)
from keystone_agents.contracts.completion import (
    build_count_request_coverage as canonical_build_count_request_coverage,
)
from keystone_agents.orchestration.completion import (
    build_count_request_coverage,
    reconcile_terminal_request_completion,
)
from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemRoute,
    WorkItemStatus,
)


def test_orchestration_reexports_the_canonical_count_contract() -> None:
    assert build_count_request_coverage is canonical_build_count_request_coverage


def test_shared_completion_decision_deduplicates_blockers_and_repair_action() -> None:
    coverage = RequestCoverage(
        interpreted_request="Compare the requested targets.",
        status="partial",
        unmet_dimensions=["missing target evidence", "missing target evidence"],
        output_form_status="satisfied",
        stop_condition_status="blocked",
        next_safe_action="Deepen the missing target evidence.",
    )

    decision = evaluate_deterministic_completion([coverage])

    assert decision.completion_allowed is False
    assert decision.reason_code == "request_contract_incomplete"
    assert decision.unmet_dimensions == ("missing target evidence",)
    assert decision.next_safe_action == "Deepen the missing target evidence."
    assert decision.blocking_count == 1


def test_shared_completion_decision_ignores_advisory_or_complete_coverage() -> None:
    advisory = RequestCoverage(
        interpreted_request="Review the result.",
        status="unassessed",
    )
    complete = RequestCoverage(
        interpreted_request="Review the result.",
        status="complete",
        output_form_status="satisfied",
        stop_condition_status="satisfied",
    )

    decision = evaluate_deterministic_completion([advisory, complete])

    assert decision.completion_allowed is True
    assert decision.reason_code == ""


def _result_with_coverage(
    coverage: dict[str, object],
    *,
    deterministic: bool = False,
    stored_only: bool = False,
    source_agent: str = WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
) -> WorkflowRunResult:
    work_item = WorkItem(
        id="wi_request_completion",
        kind="research_brief",
        title="Request completion",
        request_text="Return the requested bounded result.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
    )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="fixture:research",
        source_agent=source_agent,
        metadata={
            "request_coverage": coverage,
            **(
                {"request_coverage_enforcement": "deterministic"}
                if deterministic
                else {}
            ),
        },
    )
    return WorkflowRunResult(
        work_item=work_item.model_copy(update={"artifact_refs": [artifact]}),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[] if stored_only else [artifact],
    )


def test_count_coverage_records_explicit_underfill_without_phrase_parsing() -> None:
    coverage = build_count_request_coverage(
        interpreted_request="Return five exact matches.",
        expected_count=5,
        observed_count=2,
        item_label="matches",
        next_safe_action="Deepen the same search.",
    )

    assert coverage.status == "partial"
    assert coverage.stop_condition_status == "blocked"
    assert coverage.unmet_dimensions == [
        "requested 5 matches but only 2 met the current gates"
    ]


def test_count_coverage_treats_maximum_as_aspirational_ceiling() -> None:
    coverage = build_count_request_coverage(
        interpreted_request="Return up to five exact matches without padding.",
        expected_count=5,
        observed_count=2,
        item_label="matches",
        next_safe_action="Remove any result beyond the ceiling.",
        count_mode="maximum",
    )

    assert coverage.status == "partial"
    assert coverage.stop_condition_status == "blocked"
    assert "bounded-search exhaustion evidence" in coverage.unmet_dimensions[0]

    exhausted = build_count_request_coverage(
        interpreted_request="Return up to five exact matches without padding.",
        expected_count=5,
        observed_count=2,
        item_label="matches",
        next_safe_action="Remove any result beyond the ceiling.",
        count_mode="maximum",
        bounded_search_exhausted=True,
    )
    assert exhausted.status == "complete"
    assert exhausted.stop_condition_status == "satisfied"
    assert "exhausted qualified results" in exhausted.satisfied_dimensions[0]


def test_bounded_search_receipt_requires_completed_attempts_and_processing() -> None:
    complete = BoundedSearchReceipt(
        provider_attempt_count=3,
        planned_attempt_count=3,
        provider_completed=True,
        budget_or_deadline_stopped=False,
        discovered_candidate_count=2,
        processed_candidate_count=2,
    )
    stopped = BoundedSearchReceipt(
        provider_attempt_count=2,
        planned_attempt_count=3,
        provider_completed=False,
        budget_or_deadline_stopped=True,
        discovered_candidate_count=2,
        processed_candidate_count=1,
    )

    assert complete.exhausted is True
    assert complete.receipt()["exhausted"] is True
    assert stopped.exhausted is False


def test_bounded_search_receipt_rejects_failed_or_capped_provider_execution() -> None:
    failed = bounded_search_receipt_from_provider_telemetry(
        {
            "provider_usage": {
                "searxng": {
                    "requests_attempted": 4,
                    "requests_succeeded": 0,
                }
            },
            "search_provider_errors": [
                {"provider": "searxng", "error_type": "SearxngSearchError"}
            ],
        },
        planned_attempt_count=4,
        discovered_candidate_count=0,
        processed_candidate_count=0,
    )
    capped = bounded_search_receipt_from_provider_telemetry(
        {
            "provider_usage": {
                "exa": {
                    "requests_attempted": 1,
                    "requests_succeeded": 1,
                }
            },
            "search_provider_errors": [
                {"provider": "exa", "error_type": "ProviderRequestCapExceeded"}
            ],
        },
        planned_attempt_count=8,
        discovered_candidate_count=1,
        processed_candidate_count=1,
    )

    assert failed.provider_completed is False
    assert failed.exhausted is False
    assert capped.provider_attempt_count == 1
    assert capped.provider_completed is False
    assert capped.budget_or_deadline_stopped is True
    assert capped.exhausted is False


def test_bounded_search_receipt_uses_query_coverage_over_optional_lane_errors() -> None:
    receipt = bounded_search_receipt_from_provider_telemetry(
        {
            "query_attempt_count": 4,
            "query_completed_count": 4,
            "query_uncovered_count": 0,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": 4,
                    "requests_succeeded": 4,
                },
                "agents-web-search": {
                    "requests_attempted": 2,
                    "requests_succeeded": 2,
                },
            },
            "search_provider_errors": [
                {
                    "provider": "agents-web-search",
                    "error_type": "ProviderRequestCapExceeded",
                }
            ],
        },
        planned_attempt_count=4,
        discovered_candidate_count=3,
        processed_candidate_count=3,
    )

    assert receipt.provider_attempt_count == 6
    assert receipt.provider_completed is False
    assert receipt.query_attempt_count == 4
    assert receipt.query_completed_count == 4
    assert receipt.query_uncovered_count == 0
    assert receipt.query_ledger_valid is True
    assert receipt.budget_or_deadline_stopped is False
    assert receipt.exhausted is True


def test_bounded_search_receipt_rejects_uncovered_planned_query() -> None:
    receipt = bounded_search_receipt_from_provider_telemetry(
        {
            "query_attempt_count": 4,
            "query_completed_count": 3,
            "query_uncovered_count": 1,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": 4,
                    "requests_succeeded": 3,
                }
            },
        },
        planned_attempt_count=4,
        discovered_candidate_count=2,
        processed_candidate_count=2,
    )

    assert receipt.provider_attempt_count == 4
    assert receipt.provider_completed is False
    assert receipt.query_ledger_valid is True
    assert receipt.budget_or_deadline_stopped is True
    assert receipt.exhausted is False


def test_bounded_search_receipt_rejects_partial_query_ledger() -> None:
    receipt = bounded_search_receipt_from_provider_telemetry(
        {
            "query_attempt_count": 4,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": 4,
                    "requests_succeeded": 4,
                }
            },
        },
        planned_attempt_count=4,
        discovered_candidate_count=2,
        processed_candidate_count=2,
    )

    assert receipt.provider_attempt_count == 4
    assert receipt.query_ledger_valid is False
    assert receipt.budget_or_deadline_stopped is True
    assert receipt.exhausted is False


def test_bounded_search_receipt_rejects_query_ledger_inconsistent_with_provider_calls() -> None:
    receipt = bounded_search_receipt_from_provider_telemetry(
        {
            "query_attempt_count": 4,
            "query_completed_count": 4,
            "query_uncovered_count": 0,
            "provider_usage": {
                "searxng": {
                    "requests_attempted": 4,
                    "requests_succeeded": 0,
                }
            },
            "search_provider_errors": [
                {"provider": "searxng", "error_type": "TimeoutError"}
            ],
        },
        planned_attempt_count=4,
        discovered_candidate_count=0,
        processed_candidate_count=0,
    )

    assert receipt.query_ledger_valid is False
    assert receipt.provider_completed is False
    assert receipt.budget_or_deadline_stopped is True
    assert receipt.exhausted is False


def test_terminal_completion_blocks_hard_assessed_coverage() -> None:
    result = _result_with_coverage(
        build_count_request_coverage(
            interpreted_request="Return five exact matches.",
            expected_count=5,
            observed_count=2,
            item_label="matches",
            next_safe_action="Deepen the same search.",
        ).model_dump(mode="json"),
        deterministic=True,
    )

    reconciled = reconcile_terminal_request_completion(result)

    assert reconciled.status == WorkItemStatus.BLOCKED
    assert reconciled.next_action is not None
    assert reconciled.next_action.action == "complete_request_contract"
    assert reconciled.blockers[0].code == "request_contract_incomplete"


def test_terminal_completion_does_not_promote_advisory_partial_to_blocker() -> None:
    result = _result_with_coverage(
        {
            "interpreted_request": "Summarize the strongest available evidence.",
            "status": "partial",
            "satisfied_dimensions": ["bounded summary"],
            "unmet_dimensions": ["additional corroboration"],
            "output_form_status": "satisfied",
            "stop_condition_status": "satisfied",
            "next_safe_action": "Optionally collect another source.",
        }
    )

    reconciled = reconcile_terminal_request_completion(result)

    assert reconciled.status == WorkItemStatus.DONE
    assert reconciled.blockers == []


def test_terminal_completion_keeps_model_authored_blocked_coverage_advisory() -> None:
    result = _result_with_coverage(
        build_count_request_coverage(
            interpreted_request="Return five exact matches.",
            expected_count=5,
            observed_count=2,
            item_label="matches",
            next_safe_action="Deepen the same search.",
        ).model_dump(mode="json")
    )

    reconciled = reconcile_terminal_request_completion(result)

    assert reconciled.status == WorkItemStatus.DONE
    assert reconciled.blockers == []


def test_terminal_completion_scans_stored_artifacts_and_preserves_owner() -> None:
    result = _result_with_coverage(
        build_count_request_coverage(
            interpreted_request="Summarize three Gmail threads.",
            expected_count=3,
            observed_count=2,
            item_label="Gmail thread summaries",
            next_safe_action="Broaden the bounded Gmail query.",
        ).model_dump(mode="json"),
        deterministic=True,
        stored_only=True,
        source_agent=WorkItemRoute.GMAIL_TRIAGE.value,
    )

    reconciled = reconcile_terminal_request_completion(result)

    assert reconciled.status == WorkItemStatus.BLOCKED
    assert reconciled.next_action is not None
    assert reconciled.next_action.agent == WorkItemRoute.GMAIL_TRIAGE


def test_current_attempt_coverage_supersedes_stale_blocked_coverage() -> None:
    blocked = build_count_request_coverage(
        interpreted_request="Return three exact matches.",
        expected_count=3,
        observed_count=2,
        item_label="matches",
        next_safe_action="Deepen the same search.",
    )
    complete = build_count_request_coverage(
        interpreted_request="Return three exact matches.",
        expected_count=3,
        observed_count=3,
        item_label="matches",
        next_safe_action="Deepen the same search.",
    )
    old_artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="fixture:old-attempt",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        created_at="2026-07-26T12:00:00Z",
        metadata={
            "request_coverage": blocked.model_dump(mode="json"),
            "request_coverage_enforcement": "deterministic",
        },
    )
    repaired_artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="fixture:repaired-attempt",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        created_at="2026-07-26T12:01:00Z",
        metadata={
            "request_coverage": complete.model_dump(mode="json"),
            "request_coverage_enforcement": "deterministic",
        },
    )
    work_item = WorkItem(
        id="wi_repaired_request_completion",
        kind="research_brief",
        title="Repaired request completion",
        status=WorkItemStatus.DONE,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifact_refs=[old_artifact, repaired_artifact],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[repaired_artifact],
    )

    reconciled = reconcile_terminal_request_completion(result)

    assert reconciled.status == WorkItemStatus.DONE
    assert reconciled.blockers == []


def test_terminal_completion_is_idempotent() -> None:
    result = _result_with_coverage(
        build_count_request_coverage(
            interpreted_request="Return five exact matches.",
            expected_count=5,
            observed_count=2,
            item_label="matches",
            next_safe_action="Deepen the same search.",
        ).model_dump(mode="json"),
        deterministic=True,
    )

    once = reconcile_terminal_request_completion(result)
    twice = reconcile_terminal_request_completion(once)

    assert twice == once
