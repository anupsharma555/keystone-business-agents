from __future__ import annotations

from keystone_agents.diverse_ask_acceptance import (
    DIVERSE_ASK_ACCEPTANCE_CASES,
    DiverseAskAcceptanceCase,
    automated_proof_nodeids,
    build_diverse_ask_acceptance_report,
)
from scripts.run_diverse_ask_acceptance import finalize_proof_execution


def test_matrix_has_one_diverse_and_deterministic_case_per_major_agent() -> None:
    report = build_diverse_ask_acceptance_report()

    assert report["status"] == "complete"
    assert report["structurally_complete"] is True
    assert report["case_count"] == 16
    assert report["agent_count"] == 8
    assert report["missing_route_family_pairs"] == []
    assert report["duplicate_case_ids"] == []
    assert report["behavioral_pass_claimed"] is False


def test_each_matrix_row_has_explicit_execution_and_failure_boundaries() -> None:
    for case in DIVERSE_ASK_ACCEPTANCE_CASES:
        assert case.prompt
        assert case.required_context
        assert case.side_effect_boundary
        assert case.expected_failure_mode
        assert case.stop_condition
        assert case.output_form
        assert case.coverage_status in {"automated", "partial", "planned"}


def test_automated_rows_expose_unique_executable_pytest_nodes() -> None:
    nodeids = automated_proof_nodeids()

    assert len(nodeids) == 17
    assert len(nodeids) == len(set(nodeids))
    assert all(nodeid.startswith("tests/") and "::" in nodeid for nodeid in nodeids)


def test_automated_coverage_cannot_be_claimed_without_proof() -> None:
    try:
        DiverseAskAcceptanceCase(
            case_id="invalid",
            ask_family="diverse",
            prompt="Review this.",
            expected_route="orchestrator",
            required_context=["request"],
            allowed_tool_tier="read_only",
            side_effect_boundary="none",
            expected_failure_mode="block",
            stop_condition="stop",
            output_form="plan",
            coverage_status="automated",
        )
    except ValueError as exc:
        assert "proof ref" in str(exc)
    else:
        raise AssertionError("Automated coverage without proof refs must fail validation.")


def test_runner_claims_offline_behavior_only_when_every_row_and_proof_is_complete() -> None:
    passed = finalize_proof_execution(
        build_diverse_ask_acceptance_report(),
        proof_node_count=len(automated_proof_nodeids()),
        proofs_passed=True,
    )
    failed = finalize_proof_execution(
        build_diverse_ask_acceptance_report(),
        proof_node_count=len(automated_proof_nodeids()),
        proofs_passed=False,
    )

    assert passed["all_rows_automated"] is True
    assert passed["offline_behavioral_pass_claimed"] is True
    assert passed["behavioral_pass_claimed"] is True
    assert passed["live_pass_claimed"] is False
    assert failed["offline_behavioral_pass_claimed"] is False
    assert failed["status"] == "incomplete"
