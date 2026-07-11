"""Executable acceptance catalogue for advanced Orchestrator and Chief workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ManagerScenarioFamily = Literal[
    "multi_turn_correction",
    "selected_object_modification",
    "conflicting_instructions",
    "partial_specialist_failure",
    "evidence_disagreement",
    "approved_write_transition",
    "reversal",
    "advanced_advisory_synthesis",
    "output_receipt",
]

ADVANCED_MANAGER_GLOBAL_INVARIANTS: tuple[str, ...] = (
    "raw_operator_request_preserved_or_hashed",
    "approval_state_preserved",
    "selected_specialist_and_stop_condition_visible",
    "source_and_artifact_identity_preserved",
    "no_send_or_unapproved_write",
    "no_stale_or_duplicate_provider_action",
    "trace_and_receipt_sanitized",
)


@dataclass(frozen=True)
class AdvancedManagerScenario:
    """One no-live ANU-222 acceptance scenario and its executable proof."""

    scenario_id: str
    title: str
    family: ManagerScenarioFamily
    natural_request: str
    expected_entry_owner: str
    expected_outcome: Literal["done", "blocked", "clarification", "checkpoint"]
    required_invariants: tuple[str, ...]
    proof_nodeids: tuple[str, ...]
    live_model_required: bool = False
    external_side_effects_allowed: bool = False


ADVANCED_MANAGER_SCENARIOS: tuple[AdvancedManagerScenario, ...] = (
    AdvancedManagerScenario(
        scenario_id="AMS-01",
        title="Direct named context owner without manager ceremony",
        family="advanced_advisory_synthesis",
        natural_request=(
            "Could the preprints context agent take a read-only look at preliminary "
            "evidence for adolescent depression and return useful references?"
        ),
        expected_entry_owner="preprints_context_agent",
        expected_outcome="blocked",
        required_invariants=(
            "named_low_risk_owner_routes_directly",
            "missing_context_blocker_owned_by_specialist",
            "no_chief_wrapper",
            "no_side_effect",
        ),
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_complete_named_context_agent_ask_avoids_unneeded_chief_wrapper",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-02",
        title="Broad architecture goal remains manager owned",
        family="advanced_advisory_synthesis",
        natural_request=(
            "Review the KBA architecture and recommend the next three implementation "
            "steps without writing or posting anything."
        ),
        expected_entry_owner="chief_of_staff",
        expected_outcome="done",
        required_invariants=(
            "raw_request_preserved",
            "broad_goal_uses_manager",
            "no_side_effect",
        ),
        proof_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_orchestrator_read_contract_preserves_request_shape_and_selects_owner",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-03",
        title="Explicit correction replaces the prior route",
        family="multi_turn_correction",
        natural_request=(
            "Correction: do not scout opportunities. Research NeuroFlow only and "
            "return a read-only company brief."
        ),
        expected_entry_owner="business_research_analyst",
        expected_outcome="done",
        required_invariants=(
            "newest_direction_wins",
            "rejected_route_recorded",
            "no_send_preserved",
        ),
        proof_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_orchestrator_modify_contract_replans_after_explicit_user_correction",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-04",
        title="Pronoun-only modification blocks before mutation",
        family="selected_object_modification",
        natural_request="Update it.",
        expected_entry_owner="clarification",
        expected_outcome="clarification",
        required_invariants=(
            "zero_or_ambiguous_identity_blocks",
            "no_provider_write",
            "no_stale_object_guess",
        ),
        proof_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_orchestrator_ambiguous_modify_request_stops_before_side_effect",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-05",
        title="Selected Gmail thread survives research and draft handoff",
        family="selected_object_modification",
        natural_request=(
            "Continue with the selected Gmail thread, research the company, and "
            "prepare a reply for review without sending."
        ),
        expected_entry_owner="gmail_triage",
        expected_outcome="checkpoint",
        required_invariants=(
            "selected_thread_identity_preserved",
            "raw_body_not_persisted",
            "draft_only",
            "no_send",
        ),
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_live_gmail_retrieval_promotes_selected_thread_without_raw_body",
            "tests/test_langgraph_workflow.py::"
            "test_backend_selected_manager_loop_uses_graph_for_gmail_research_outreach_checkpoint",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-06",
        title="New operator direction supersedes stale workflow context",
        family="conflicting_instructions",
        natural_request=(
            "Use the latest instruction: research only. Ignore the older outreach "
            "step and do not draft or send anything."
        ),
        expected_entry_owner="business_research_analyst",
        expected_outcome="done",
        required_invariants=(
            "latest_instruction_authoritative",
            "stale_outreach_not_executed",
            "no_send_preserved",
        ),
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_manager_loop_review_uses_latest_thread_followup_for_any_route",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-07",
        title="Partial failure preserves completed specialist evidence",
        family="partial_specialist_failure",
        natural_request=(
            "Keep completed research, identify the failed child step, and continue "
            "only the safe remaining work without repeating completed actions."
        ),
        expected_entry_owner="chief_of_staff",
        expected_outcome="blocked",
        required_invariants=(
            "completed_evidence_preserved",
            "failed_child_identified",
            "completed_provider_action_not_duplicated",
            "bounded_repair_only",
        ),
        proof_nodeids=(
            "tests/test_langgraph_workflow.py::"
            "test_backend_selected_checkpoint_approval_resume_reports_saved_state",
            "tests/test_workflow_runner.py::"
            "test_manager_loop_stops_before_repeating_specialist_for_orchestrator_workflow",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-08",
        title="Contradictory evidence remains reviewable",
        family="evidence_disagreement",
        natural_request=(
            "Compare the retained and contradictory sources, explain the conflict, "
            "and do not treat the conclusion as approved fact."
        ),
        expected_entry_owner="business_research_analyst",
        expected_outcome="done",
        required_invariants=(
            "contradiction_visible",
            "unsupported_claim_not_promoted",
            "review_state_preserved",
        ),
        proof_nodeids=(
            "tests/test_source_triage.py::"
            "test_source_triage_rejects_contradictory_formal_opportunity_source",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-09",
        title="Exact Airtable write transitions to owning agent gate",
        family="approved_write_transition",
        natural_request=(
            "Using Airtable context, create one marked test expense in the named "
            "table, verify it, update the same record, and remove the test record."
        ),
        expected_entry_owner="airtable_context_agent",
        expected_outcome="checkpoint",
        required_invariants=(
            "owning_agent_selected",
            "exact_scope_only",
            "direct_operator_scope_not_reapproved",
            "read_back_required",
            "marker_restricted_cleanup",
        ),
        proof_nodeids=(
            "tests/test_manual_request_plan.py::"
            "test_context_agent_internal_update_routes_to_owner_for_approval_gating",
            "tests/test_airtable_test_base_lifecycle.py::"
            "test_test_base_lifecycle_is_dry_run_by_default",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-10",
        title="Operator reversal removes a pending write",
        family="reversal",
        natural_request=(
            "Correction: do not create a document. Review the project context and "
            "list the missing evidence only."
        ),
        expected_entry_owner="chief_of_staff",
        expected_outcome="done",
        required_invariants=(
            "pending_write_removed",
            "stale_write_cannot_execute",
            "read_only_result",
        ),
        proof_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_chief_modify_contract_revises_plan_after_new_operator_direction",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-11",
        title="Manager stages context agents before durable specialist",
        family="advanced_advisory_synthesis",
        natural_request=(
            "Use Preprints and Zotero context before Business Research reviews "
            "NeuroFlow; return the best next owner with all writes blocked."
        ),
        expected_entry_owner="chief_of_staff",
        expected_outcome="checkpoint",
        required_invariants=(
            "context_handoffs_typed",
            "durable_owner_distinct",
            "nested_tools_advisory",
            "no_write_inheritance",
        ),
        proof_nodeids=(
            "tests/test_manager_rwm_acceptance.py::"
            "test_chief_structured_handoff_keeps_nested_specialist_advisory",
        ),
    ),
    AdvancedManagerScenario(
        scenario_id="AMS-12",
        title="Completion receipt exposes review without false blocking",
        family="output_receipt",
        natural_request=(
            "Find behavioral health AI opportunities and return the result, review "
            "status, evidence boundary, and next safe action."
        ),
        expected_entry_owner="opportunity_scout",
        expected_outcome="done",
        required_invariants=(
            "status_visible",
            "review_decision_visible",
            "generic_review_gap_not_blocking",
            "next_safe_action_visible",
        ),
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_manager_loop_generic_review_gaps_do_not_block_fixture_artifacts",
        ),
    ),
)


def get_advanced_manager_scenario(scenario_id: str) -> AdvancedManagerScenario:
    """Return one scenario by stable identifier."""

    for scenario in ADVANCED_MANAGER_SCENARIOS:
        if scenario.scenario_id == scenario_id:
            return scenario
    raise KeyError(scenario_id)
