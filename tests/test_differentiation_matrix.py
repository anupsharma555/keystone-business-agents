from __future__ import annotations

from keystone_agents.differentiation_matrix import (
    differentiation_comparison_matrix,
    differentiation_validation_milestones,
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
