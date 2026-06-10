from __future__ import annotations

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.skill_evals import (
    SUPPORTED_SURFACES,
    load_skill_task_eval_cases,
    run_skill_task_eval_suite,
)
from keystone_agents.skill_sets import explain_agent_skill_selection


def test_skill_task_matrix_covers_agents_and_surfaces() -> None:
    cases = load_skill_task_eval_cases()

    assert {case.agent for case in cases} == set(AGENT_REGISTRY)
    assert {"slack", "computer"} <= {case.surface for case in cases}
    assert {case.surface for case in cases} <= SUPPORTED_SURFACES
    for case in cases:
        assert case.request
        assert case.expected["include_skills"]
        assert case.expected["exclude_skills"]


def test_skill_task_matrix_selection_and_visibility_passes() -> None:
    summary = run_skill_task_eval_suite()

    assert summary.failed == 0, summary.to_dict()
    assert summary.passed == summary.total
    for result in summary.results:
        assert set(result.expected_skills) <= set(result.selected_skills)
        assert not (set(result.excluded_skills) & set(result.selected_skills))
        assert set(result.selected_skills) == set(result.selection_reasons)
        for skill_name in result.expected_skills:
            assert result.selection_reasons[skill_name]


def test_skill_task_matrix_can_filter_by_surface() -> None:
    slack_summary = run_skill_task_eval_suite(surfaces=("slack",))
    computer_summary = run_skill_task_eval_suite(surfaces=("computer",))

    assert slack_summary.failed == 0
    assert computer_summary.failed == 0
    assert slack_summary.total > 0
    assert computer_summary.total > 0
    assert {result.surface for result in slack_summary.results} == {"slack"}
    assert {result.surface for result in computer_summary.results} == {"computer"}


def test_skill_selection_explains_route_trigger_and_context_reasons() -> None:
    reasons = explain_agent_skill_selection(
        "gmail_triage",
        request_text="Save a source-cited report with missing evidence.",
        context_flags={"needs_workspace_artifact": True},
    )

    assert "core" in reasons["context_permission_gating"]
    assert "specialist" in reasons["gmail_triage_specialist_contracts"]
    assert "route_default" in reasons["workflow_lifecycle_tracking"]
    assert any(
        reason.startswith("request_trigger:")
        for reason in reasons["evidence_attribution_and_claim_mapping"]
    )
    assert "context_flag:needs_workspace_artifact" in reasons["workspace_artifact_governance"]
