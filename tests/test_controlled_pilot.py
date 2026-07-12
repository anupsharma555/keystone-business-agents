from __future__ import annotations

from dataclasses import replace

import pytest

from keystone_agents.controlled_pilot import (
    ControlledPilotObservation,
    assess_controlled_pilot_observation,
    controlled_pilot_cases,
    controlled_pilot_natural_request_sha256,
    controlled_pilot_ready,
)


def _observation(case_id: str, **overrides: object) -> ControlledPilotObservation:
    case = next(item for item in controlled_pilot_cases() if item.case_id == case_id)
    values = {
        "case_id": case_id,
        "natural_request_sha256": controlled_pilot_natural_request_sha256(case),
        "slack_permalink_present": True,
        "answer_first": True,
        "human_review_passed": True,
        "final_response_count": 1,
        "route_correct": True,
        "graph_used": case.backend == "langgraph",
        "work_item_continuity": True,
        "visible_source_count": 2,
        "context_reentry_fields": 0,
        "manual_provider_ids": 0,
        "approval_round_trips": 1 if case.allowed_provider_writes else 0,
        "provider_writes": case.allowed_provider_writes,
        "provider_readback_verified": case.allowed_provider_writes > 0,
        "cleanup_verified": not case.requires_cleanup,
        "unintended_writes": 0,
        "duplicate_artifacts": 0,
        "developer_intervention": False,
        "openai_requests": case.max_openai_requests,
        "estimated_cost_usd": case.max_cost_usd,
        "latency_ms": 1_000,
        "evidence_refs": ("slack:permalink", "trace:run-1"),
    }
    values.update(overrides)
    return ControlledPilotObservation(**values)


def test_catalog_has_four_representative_workflows_and_direct_graph_split() -> None:
    cases = controlled_pilot_cases()

    assert {case.case_id for case in cases} == {
        "research_to_internal_doc",
        "selected_gmail_thread_followup",
        "current_opportunity_assessment",
        "weekly_project_brief",
    }
    assert [case.backend for case in cases].count("direct_specialist") == 1
    assert [case.backend for case in cases].count("langgraph") == 3
    assert all(case.max_openai_requests > 0 and case.max_cost_usd > 0 for case in cases)
    assert all(case.allowed_provider_writes == 0 for case in cases)
    assert next(
        case.expected_entry_owner
        for case in cases
        if case.case_id == "selected_gmail_thread_followup"
    ) == "orchestrator"
    gmail_ask = next(
        case.natural_ask for case in cases if case.case_id == "selected_gmail_thread_followup"
    )
    assert "latest Gmail thread from the configured exact test sender" in gmail_ask
    assert "original inquiry and all messages" in gmail_ask
    assert "Using only that complete thread and approved KNI context" in gmail_ask
    assert "return the result here for review" in gmail_ask
    assert "provider ID" not in gmail_ask


def test_live_pilot_waits_for_all_four_trusted_runtime_rows() -> None:
    rows = {row: "pass" for row in ("L174-14", "L174-16", "L174-19", "L174-20")}

    assert controlled_pilot_ready(rows) is True
    rows["L174-19"] = "partial"
    assert controlled_pilot_ready(rows) is False


@pytest.mark.parametrize("case_id", [case.case_id for case in controlled_pilot_cases()])
def test_each_case_passes_only_with_answer_first_safe_auditable_receipt(case_id: str) -> None:
    result = assess_controlled_pilot_observation(_observation(case_id))

    assert result.status == "pass"
    assert result.failed_checks == ()


def test_direct_specialist_case_fails_if_graph_is_used_without_need() -> None:
    result = assess_controlled_pilot_observation(
        _observation("current_opportunity_assessment", graph_used=True)
    )

    assert result.status == "fail"
    assert result.failed_checks == ("backend_matches",)


def test_duplicate_friction_hidden_ids_or_extra_write_fail_the_pilot() -> None:
    result = assess_controlled_pilot_observation(
        _observation(
            "selected_gmail_thread_followup",
            manual_provider_ids=1,
            approval_round_trips=2,
            provider_writes=1,
            duplicate_artifacts=1,
        )
    )

    assert result.status == "fail"
    assert set(result.failed_checks) == {
        "provider_ids_internal",
        "approval_scope_exact",
        "write_scope_exact",
        "no_duplicate_artifacts",
    }


def test_human_copy_review_is_required_for_a_pilot_pass() -> None:
    result = assess_controlled_pilot_observation(
        _observation("selected_gmail_thread_followup", human_review_passed=False)
    )

    assert result.status == "fail"
    assert result.failed_checks == ("human_review_passed",)


def test_initial_pilot_rejects_provider_write_even_for_doc_ready_research() -> None:
    result = assess_controlled_pilot_observation(
        _observation("research_to_internal_doc", provider_writes=1)
    )

    assert result.status == "fail"
    assert result.failed_checks == ("write_scope_exact",)


def test_unknown_case_or_noncanonical_ask_cannot_be_scored() -> None:
    with pytest.raises(ValueError, match="Unknown controlled pilot case"):
        assess_controlled_pilot_observation(
            replace(_observation("weekly_project_brief"), case_id="x")
        )
    with pytest.raises(ValueError, match="unchanged natural ask"):
        assess_controlled_pilot_observation(
            _observation("weekly_project_brief", natural_request_sha256="a" * 64)
        )
