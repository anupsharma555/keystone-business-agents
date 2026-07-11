from __future__ import annotations

from keystone_agents.differentiation_matrix import (
    DifferentiationObservation,
    compare_differentiation_observations,
    differentiation_commitments,
    differentiation_comparison_matrix,
    differentiation_validation_milestones,
    observation_is_safe_and_useful,
)

EXPECTED_DIFFERENTIATORS = {
    "persistent_workitem_state",
    "slack_native_execution",
    "source_backed_specialists",
    "durable_approvals_audit",
    "typed_context_handoffs",
    "operational_follow_through",
}


def test_differentiation_matrix_covers_anu_175_surface() -> None:
    cases = differentiation_comparison_matrix()

    assert {case.case_id for case in cases} == EXPECTED_DIFFERENTIATORS
    assert len({case.case_id for case in cases}) == len(cases)
    for case in cases:
        assert case.differentiator
        assert case.chatgpt_codex_baseline
        assert case.kba_value
        assert case.current_surfaces
        assert case.offline_validation
        assert case.later_live_probe
        assert case.current_capability
        assert case.near_term_proof
        assert case.future_boundary
        assert "ANU-175" in case.owner_issues
        assert all(issue.startswith("ANU-") for issue in case.owner_issues)


def test_each_differentiator_has_offline_and_later_live_proof_boundary() -> None:
    offline_terms = (
        "pytest",
        "fixture",
        "test",
        "tests",
        "local eval",
        "review",
        "validation",
    )

    for case in differentiation_comparison_matrix():
        offline_text = " ".join(case.offline_validation).lower()
        assert any(term in offline_text for term in offline_terms)
        assert "live" in case.later_live_probe.lower() or "Slack" in case.later_live_probe
        assert "Do not" in case.future_boundary


def test_validation_milestones_stage_live_work_after_offline_gate() -> None:
    milestones = differentiation_validation_milestones()

    assert [milestone.milestone_id for milestone in milestones] == [
        "strategy_matrix",
        "no_live_execution_proof",
        "live_slack_api_probe",
    ]
    assert milestones[0].owner_issues == ("ANU-175",)

    no_live = milestones[1]
    no_live_text = " ".join(
        no_live.offline_exit_criteria
        + no_live.later_live_entry_criteria
        + no_live.must_not_do
    ).lower()
    assert "offline gate is clean" in no_live_text
    assert "budget" in no_live_text
    assert "no live side effects" in no_live_text

    live_probe = milestones[2]
    live_text = " ".join(
        live_probe.later_live_entry_criteria + live_probe.must_not_do
    ).lower()
    assert "operator approval" in live_text
    assert "budget" in live_text
    assert "without explicit scope" in live_text


def test_milestones_have_evidence_and_no_side_effect_guards() -> None:
    for milestone in differentiation_validation_milestones():
        assert milestone.name
        assert milestone.offline_exit_criteria
        assert milestone.later_live_entry_criteria
        assert milestone.required_evidence
        assert milestone.must_not_do
        assert all(issue.startswith("ANU-") for issue in milestone.owner_issues)


def _observation(system: str, **overrides: object) -> DifferentiationObservation:
    values = {
        "system": system,
        "workflow_id": "selected_gmail_thread_followup",
        "natural_request_sha256": "a" * 64,
        "useful_result": True,
        "route_correct": True,
        "sources_visible": True,
        "followup_continuity": True,
        "context_reentry_fields": 0,
        "manual_provider_ids": 0,
        "approval_round_trips": 1,
        "unintended_writes": 0,
        "duplicate_artifacts": 0,
        "developer_intervention": False,
        "latency_ms": 1200,
        "estimated_cost_usd": 0.02,
        "evidence_refs": ("test:manager_stateful_followup",),
    }
    values.update(overrides)
    return DifferentiationObservation(**values)


def test_commitments_select_five_measurable_near_term_claims() -> None:
    commitments = differentiation_commitments()

    assert len(commitments) == 5
    assert {commitment.case_id for commitment in commitments} <= EXPECTED_DIFFERENTIATORS
    for commitment in commitments:
        assert commitment.product_commitment
        assert commitment.representative_workflows
        assert commitment.required_metrics
        assert commitment.minimum_proof
        assert {"ANU-10", "ANU-125", "ANU-175"} <= set(commitment.owner_issues)


def test_safe_offline_observation_proves_no_side_effect_execution_boundary() -> None:
    observation = _observation("kba")

    assert observation_is_safe_and_useful(observation) is True
    assert observation.unintended_writes == 0
    assert observation.duplicate_artifacts == 0
    assert observation.developer_intervention is False


def test_comparison_supports_claim_only_for_matched_direct_evidence() -> None:
    kba = _observation("kba")
    baseline = _observation(
        "codex_chatgpt_baseline",
        sources_visible=False,
        followup_continuity=False,
        context_reentry_fields=3,
        manual_provider_ids=1,
        approval_round_trips=2,
        evidence_refs=("baseline:controlled_run",),
    )

    comparison = compare_differentiation_observations(kba, baseline)

    assert comparison.status == "supported"
    assert comparison.kba_safe_and_useful is True
    assert set(comparison.improvements) == {
        "context_reentry_fields",
        "manual_provider_ids",
        "approval_round_trips",
        "sources_visible",
        "followup_continuity",
    }
    assert comparison.regressions == ()


def test_comparison_rejects_mismatched_ask_instead_of_inventing_baseline() -> None:
    kba = _observation("kba")
    baseline = _observation(
        "codex_chatgpt_baseline",
        natural_request_sha256="b" * 64,
        evidence_refs=("baseline:different_run",),
    )

    try:
        compare_differentiation_observations(kba, baseline)
    except ValueError as exc:
        assert "same workflow and natural ask" in str(exc)
    else:
        raise AssertionError("Expected mismatched comparison to fail")


def test_comparison_does_not_support_unsafe_or_regressive_kba_result() -> None:
    kba = _observation("kba", unintended_writes=1, sources_visible=False)
    baseline = _observation(
        "codex_chatgpt_baseline",
        context_reentry_fields=2,
        evidence_refs=("baseline:controlled_run",),
    )

    comparison = compare_differentiation_observations(kba, baseline)

    assert comparison.status == "not_supported"
    assert comparison.kba_safe_and_useful is False
    assert set(comparison.regressions) == {"sources_visible", "unintended_writes"}
