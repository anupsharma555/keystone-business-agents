# ruff: noqa: E501
from __future__ import annotations

import json

import pytest

import scripts.compare_langgraph_quality as compare_script
from keystone_agents.langgraph_quality import (
    compare_user_facing_output_quality,
    finalize_langgraph_live_output_review,
    langgraph_edge_program_inventory,
    langgraph_live_smoke_plan,
    langgraph_live_smoke_plan_from_packet,
    langgraph_live_user_facing_output_quality,
    langgraph_llm_reasoning_touchpoints,
    langgraph_open_smoke_checkpoint,
    render_langgraph_edge_program_inventory,
    render_langgraph_live_output_review_decision,
    render_langgraph_live_output_review_packet,
    render_langgraph_live_output_review_rubric,
    render_langgraph_live_smoke_plan,
    render_langgraph_open_smoke_checkpoint,
    user_facing_output_quality_scorecard,
    validate_langgraph_edge_program_inventory,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemEvent,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from scripts.compare_langgraph_quality import SCENARIOS, main, run_all_scenarios, run_comparison

NO_SIDE_EFFECTS = {
    "send_attempted": False,
    "gmail_draft_created": False,
    "external_post_created": False,
    "schedule_created": False,
    "external_write_performed": False,
}


def test_matched_northstar_visible_output_scorecard_favors_graph_terminal_brief() -> None:
    control_summary = (
        "*Answer:*\nJordan asked about evaluation design.\n\n"
        "*Organization context:*\nNorthstar provides workflow analytics for "
        "behavioral-health clinics.\n\n"
        "*Recommended next step:*\nReview a draft before external use.\n\n"
        "*Suggested reply:*\nHi Jordan, would a brief conversation be useful?\n\n"
        "*Supporting evidence and approval status:*\nDraft-only; no send occurred."
    )
    graph_summary = (
        "*Recommendation:*\nProceed with a brief exploratory reply after human review.\n\n"
        "*Strongest evidence:*\n"
        "- Northstar provides workflow analytics for behavioral-health clinics.\n"
        "- Jordan asked whether Keystone could advise on evaluation design.\n\n"
        "*Main uncertainty:*\n- The exact workflow and intended users are unknown.\n\n"
        "*Next step:*\nReview the draft, then ask for a short exploratory conversation.\n\n"
        "*Draft for review:*\nHi Jordan, would a brief conversation be useful?"
    )
    kwargs = {
        "expected_target_terms": ("Northstar", "evaluation design"),
        "openai_requests": 1,
        "total_tokens": 42200,
        "max_openai_requests": 1,
        "max_total_tokens": 50000,
        "side_effects": NO_SIDE_EFFECTS,
    }

    control = user_facing_output_quality_scorecard(control_summary, **kwargs)
    graph = user_facing_output_quality_scorecard(graph_summary, **kwargs)
    comparison = compare_user_facing_output_quality(control, graph)

    assert control["passed"] is False
    assert graph["passed"] is True
    assert graph["total_score"] == 16
    assert comparison["score_delta"] > 0
    assert comparison["graph_quality_improved"] is True
    assert comparison["dimension_deltas"]["request_token_efficiency"] == 0
    assert comparison["dimension_deltas"]["side_effect_safety"] == 0


def test_matched_gmail_visible_output_scorecard_favors_graph_terminal_brief() -> None:
    control_summary = (
        "Heading: Draft email for synthetic validation correspondent\n\n"
        "Body: Thanks for the packet. Would a brief conversation be useful?\n\n"
        "Safety: No Gmail draft was created and no message was sent."
    )
    graph_summary = (
        "*Recommendation:*\nUse the thread-local reply for human review only.\n\n"
        "*Strongest evidence:*\n"
        "- The correspondence is synthetic KBA validation material.\n"
        "- The approved objective is one concise confirmation reply.\n\n"
        "*Main uncertainty:*\n- No real client relationship is supported.\n\n"
        "*Next step:*\nReview the draft; no Gmail/provider draft or send is authorized.\n\n"
        "*Draft for review:*\nThanks for the validation packet. "
        "Would a brief conversation be useful?"
    )
    kwargs = {
        "expected_target_terms": ("validation", "Gmail"),
        "openai_requests": 1,
        "total_tokens": 38158,
        "max_openai_requests": 1,
        "max_total_tokens": 50000,
        "side_effects": NO_SIDE_EFFECTS,
    }

    control = user_facing_output_quality_scorecard(control_summary, **kwargs)
    graph = user_facing_output_quality_scorecard(graph_summary, **kwargs)
    comparison = compare_user_facing_output_quality(control, graph)

    assert graph["passed"] is True
    assert graph["dimensions"]["side_effect_safety"] == 2
    assert graph["diagnostics"]["backend_terms"] == []
    assert comparison["graph_quality_improved"] is True


def test_langgraph_edge_program_inventory_names_selected_durable_edges() -> None:
    inventory = langgraph_edge_program_inventory()
    validation = validate_langgraph_edge_program_inventory(
        inventory,
        known_quality_scenarios=SCENARIOS,
    )
    report = render_langgraph_edge_program_inventory(inventory)

    selected_ids = {item["edge_id"] for item in inventory["selected_edges"]}
    assert inventory["schema"] == "keystone.langgraph.edge_program_inventory.v1"
    assert selected_ids == {
        "business_research_opportunity_outreach_checkpoint",
        "gmail_research_outreach_checkpoint",
        "rss_preprints_zotero_signal_to_research_or_opportunity",
        "chief_managed_coordination",
    }
    assert validation == {
        "schema": "keystone.langgraph.edge_program_validation.v1",
        "valid": True,
        "blockers": [],
        "selected_edge_count": 4,
        "future_edge_count": 1,
    }
    assert "Selected durable edges: 4" in report
    assert "Inventory valid: yes" in report
    assert "Default LLM manager-review API calls: no" in report
    assert "Optional LLM reasoning touchpoints: 4" in report
    assert (
        "LLM review evidence fields: review_mode, llm_review_used, cost_guard, "
        "deterministic_gates_authoritative"
    ) in report
    assert "Evidence open_default_backend_selected: 12 fields" in report
    assert "Evidence forced_langgraph_false_control: 12 fields" in report
    assert (
        "Evidence forced_langgraph_true: 13 fields; extra: langgraph_orchestration_event"
        in report
    )
    assert "Workflow SDK usage evidence: usage=requests" in report
    assert "cost=estimated_usd_or_amount_usd_or_source" in report
    assert (
        "request_cache=static_prefix_sha256_or_dynamic_prompt_sha256_or_prompt_cache_key_hash"
        in report
    )
    assert "Side-effect evidence: reviewed=true and all flags present and false:" in report
    assert "send_attempted" in report
    assert "external_write_performed" in report
    context_edge = next(
        item
        for item in inventory["selected_edges"]
        if item["edge_id"] == "rss_preprints_zotero_signal_to_research_or_opportunity"
    )
    assert context_edge["quality_scenarios"] == [
        "rss-opportunity",
        "preprints-zotero-research",
    ]
    chief_edge = next(
        item
        for item in inventory["selected_edges"]
        if item["edge_id"] == "chief_managed_coordination"
    )
    assert chief_edge["quality_scenarios"] == [
        "chief-context-opportunity",
        "research-opportunity",
        "gmail-research-outreach",
    ]
    assert chief_edge["handoff_contracts"] == [
        "ChiefOfStaffResult.durable_handoff.agent selects canonical WorkItem specialist",
        "ChiefOfStaffResult.context_handoffs selects read-only context staging agents",
        "legacy Chief of Staff -> Agent prose remains compatibility fallback only",
        "agents_as_tools advisory output must not replace durable graph specialist nodes",
    ]


def test_langgraph_llm_reasoning_touchpoints_are_offline_and_authority_bounded() -> None:
    policy = langgraph_llm_reasoning_touchpoints()

    assert policy["schema"] == "keystone.langgraph.llm_reasoning_touchpoints.v1"
    assert policy["default_api_calls"] is False
    assert policy["deterministic_gates_authoritative"] is True
    assert policy["evidence_fields"] == [
        "review_mode",
        "llm_review_used",
        "cost_guard",
        "deterministic_gates_authoritative",
    ]
    touchpoints = {item["touchpoint"]: item for item in policy["model_reasoning_may_run_at"]}
    assert set(touchpoints) == {
        "relevance_usefulness_review",
        "repair_routing",
        "source_sufficiency_judgment",
        "final_answer_satisfaction",
    }
    assert all(item["may_use_llm"] is True for item in touchpoints.values())
    assert all(item["default_model_call"] is False for item in touchpoints.values())
    assert "approval_checkpoint" in policy["deterministic_only_nodes"]
    assert "explicit_fake_or_live_review_mode_with_cost_budget" in policy["trigger_policy"]
    assert policy["manager_loop_repair_context_fields"] == [
        "observed_gaps",
        "recommended_next_step",
        "target_output_type",
        "source_issue",
        "repair_route",
        "qualitative_feedback",
        "review_mode",
        "llm_review_used",
        "cost_guard",
    ]


def test_langgraph_edge_program_inventory_requires_llm_reasoning_policy() -> None:
    inventory = langgraph_edge_program_inventory()
    inventory["llm_reasoning_policy"]["default_api_calls"] = True

    validation = validate_langgraph_edge_program_inventory(inventory)

    assert validation["valid"] is False
    assert "llm_reasoning_policy_changed" in validation["blockers"]


def test_langgraph_edge_program_inventory_rejects_unknown_quality_scenarios() -> None:
    inventory = langgraph_edge_program_inventory()
    inventory["selected_edges"][0]["quality_scenarios"] = ["missing-scenario"]

    validation = validate_langgraph_edge_program_inventory(
        inventory,
        known_quality_scenarios=SCENARIOS,
    )

    assert validation["valid"] is False
    assert (
        "business_research_opportunity_outreach_checkpoint:"
        "unknown_quality_scenarios:missing-scenario"
    ) in validation["blockers"]


def test_langgraph_edge_program_inventory_preserves_live_smoke_boundary() -> None:
    inventory = langgraph_edge_program_inventory()
    boundary = inventory["live_smoke_boundary"]

    assert boundary["max_live_sdk_calls"] == 5
    assert boundary["serial_only"] is True
    assert boundary["offline_first"] is True
    assert boundary["comparison_modes"] == [
        "open_default_backend_selected",
        "forced_langgraph_false_control",
        "forced_langgraph_true",
    ]
    base_evidence = [
        "actual_environment",
        "slack_permalink",
        "work_item_id",
        "route",
        "status",
        "operator_status",
        "slack_display_title",
        "visible_output",
        "source_urls",
        "side_effects",
        "side_effects_reviewed",
        "workflow_sdk_usage_event",
    ]
    assert boundary["mode_evidence_requirements"] == {
        "open_default_backend_selected": base_evidence,
        "forced_langgraph_false_control": base_evidence,
        "forced_langgraph_true": base_evidence + ["langgraph_orchestration_event"],
    }
    assert boundary["workflow_sdk_usage_requirements"] == {
        "schema": "keystone.workflow_sdk_usage.v1",
        "usage": ["requests"],
        "cost": ["estimated_usd_or_amount_usd_or_source"],
        "request_cache": [
            "static_prefix_sha256_or_dynamic_prompt_sha256_or_prompt_cache_key_hash"
        ],
    }
    assert boundary["side_effect_requirements"] == {
        "all_flags_present": True,
        "all_flags_false": True,
        "reviewed_field": "side_effects_reviewed",
        "reviewed_value": True,
        "flags": [
            "send_attempted",
            "gmail_draft_created",
            "external_post_created",
            "schedule_created",
            "external_write_performed",
        ],
    }
    assert boundary["first_run_gate"] == (
        "open_default_backend_selected must pass before forced modes"
    )
    assert boundary["live_search_default"] is False
    assert boundary["external_writes_enabled"] is False
    assert boundary["send_enabled"] is False
    for item in inventory["selected_edges"]:
        assert set(item["preserved_invariants"]) == set(inventory["required_invariants"])
        assert item["test_paths"]
        assert item["doc_refs"]


def test_all_selected_langgraph_scenarios_are_ready_offline(tmp_path) -> None:
    report = run_all_scenarios(database_dir=tmp_path)

    assert report["schema"] == "keystone.langgraph.all_scenarios_readiness.v1"
    assert report["ready"] is True
    assert report["blockers"] == []
    assert report["scenario_count"] == len(report["selected_scenarios"])
    assert set(report["selected_scenarios"]) == {
        "research-opportunity",
        "outreach-checkpoint",
        "gmail-research-outreach",
        "gmail-research-thread-draft",
        "rss-opportunity",
        "preprints-zotero-research",
        "chief-context-opportunity",
    }
    assert report["missing_scenarios"] == []
    assert report["not_ready_scenarios"] == []
    assert report["regressed_scenarios"] == []
    assert all(item["ready_for_live_smoke"] for item in report["scenarios"])
    assert all(not item["regression_markers"] for item in report["scenarios"])


def test_langgraph_edge_program_inventory_requires_live_evidence_contract() -> None:
    inventory = langgraph_edge_program_inventory()
    inventory["live_smoke_boundary"]["mode_evidence_requirements"][
        "forced_langgraph_true"
    ] = ["workflow_sdk_usage_event"]

    validation = validate_langgraph_edge_program_inventory(inventory)

    assert validation["valid"] is False
    assert (
        "live_smoke_boundary:forced_langgraph_true:evidence_requirements_changed"
        in validation["blockers"]
    )


def test_langgraph_edge_program_inventory_requires_usage_evidence_contract() -> None:
    inventory = langgraph_edge_program_inventory()
    inventory["live_smoke_boundary"]["workflow_sdk_usage_requirements"] = {
        "schema": "keystone.workflow_sdk_usage.v1"
    }

    validation = validate_langgraph_edge_program_inventory(inventory)

    assert validation["valid"] is False
    assert "live_smoke_boundary:workflow_sdk_usage_requirements_changed" in (
        validation["blockers"]
    )


def test_langgraph_edge_program_inventory_requires_side_effect_contract() -> None:
    inventory = langgraph_edge_program_inventory()
    inventory["live_smoke_boundary"]["side_effect_requirements"] = {
        "all_flags_present": True,
        "all_flags_false": True,
        "flags": ["send_attempted"],
    }

    validation = validate_langgraph_edge_program_inventory(inventory)

    assert validation["valid"] is False
    assert "live_smoke_boundary:side_effect_requirements_changed" in (
        validation["blockers"]
    )


def test_compare_script_can_print_edge_program_inventory_json(capsys) -> None:
    exit_code = main(["--edge-inventory", "--json", "--require-ready"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["inventory"]["schema"] == "keystone.langgraph.edge_program_inventory.v1"
    assert payload["validation"]["valid"] is True
    assert payload["inventory"]["live_smoke_boundary"]["max_live_sdk_calls"] == 5
    assert payload["inventory"]["selected_edges"][0]["edge_id"] == (
        "business_research_opportunity_outreach_checkpoint"
    )


def test_compare_script_can_run_all_inventory_scenarios_json(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "--all-scenarios",
            "--database-dir",
            str(tmp_path),
            "--json",
            "--require-ready",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["schema"] == "keystone.langgraph.all_scenarios_readiness.v1"
    assert payload["ready"] is True
    assert payload["blockers"] == []
    assert payload["scenario_count"] == len(payload["selected_scenarios"])
    assert {item["scenario"] for item in payload["scenarios"]} == set(
        payload["selected_scenarios"]
    )


def test_compare_langgraph_quality_script_runs_offline_default(tmp_path) -> None:
    output = run_comparison(database_dir=tmp_path)

    assert output["schema"] == "keystone.langgraph.quality_comparison_run.v1"
    assert output["control"]["quality_markers"]["graph_explainability"] is False
    assert output["graph"]["quality_markers"]["graph_explainability"] is True
    assert output["comparison"]["same_route"] is True
    assert output["comparison"]["same_status"] is True
    assert output["comparison"]["side_effect_safe_both"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert "requested_stage_trace_added" in output["comparison"]["improvement_markers"]
    assert output["comparison"]["regression_markers"] == []
    assert "Ready for bounded live smoke: yes" in output["report"]
    assert "Graph route matches expected: n/a" in output["report"]
    assert output["live_output_review_rubric"]["schema"] == (
        "keystone.langgraph.live_output_review_rubric.v1"
    )
    assert {"usefulness", "relevance", "evidence_quality"} <= {
        item["criterion"] for item in output["live_output_review_rubric"]["criteria"]
    }


def test_compare_langgraph_quality_script_runs_rss_context_scenario(tmp_path) -> None:
    output = run_comparison(scenario="rss-opportunity", database_dir=tmp_path)

    assert output["scenario"] == "rss-opportunity"
    assert output["control"]["quality_markers"]["route"] == "rss_context_agent"
    assert output["graph"]["quality_markers"]["route"] == "opportunity_scout"
    assert output["comparison"]["same_route"] is False
    assert output["comparison"]["route_change_allowed"] is True
    assert output["comparison"]["graph_route_matches_expected"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert "graph_enabled_expected_route" in output["comparison"]["improvement_markers"]
    assert "context_evidence_added" in output["comparison"]["improvement_markers"]
    assert output["comparison"]["regression_markers"] == []
    assert "Route change allowed: yes" in output["report"]
    assert "Graph route matches expected: yes" in output["report"]


def test_compare_langgraph_quality_script_runs_preprints_zotero_context_scenario(
    tmp_path,
) -> None:
    output = run_comparison(scenario="preprints-zotero-research", database_dir=tmp_path)

    graph_markers = output["graph"]["quality_markers"]
    assert output["scenario"] == "preprints-zotero-research"
    assert graph_markers["route"] == "business_research_analyst"
    assert graph_markers["status"] == "done"
    assert {"preprints_context_summary", "zotero_context_summary", "company_profile"} <= set(
        graph_markers["artifact_types"]
    )
    assert {"preprints_context_agent", "zotero_context_agent"} <= set(
        graph_markers["source_providers"]
    )
    assert output["comparison"]["route_change_allowed"] is True
    assert output["comparison"]["graph_route_matches_expected"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert output["comparison"]["context_evidence_delta"] >= 2
    assert output["comparison"]["durable_stage_delta"] >= 2
    assert output["comparison"]["regression_markers"] == []
    assert "context_evidence_added" in output["comparison"]["improvement_markers"]


def test_compare_langgraph_quality_script_runs_chief_context_opportunity_scenario(
    tmp_path,
) -> None:
    output = run_comparison(scenario="chief-context-opportunity", database_dir=tmp_path)

    control_markers = output["control"]["quality_markers"]
    graph_markers = output["graph"]["quality_markers"]
    assert output["scenario"] == "chief-context-opportunity"
    assert control_markers["route"] == "opportunity_scout"
    assert control_markers["status"] == "blocked"
    assert graph_markers["route"] == "opportunity_scout"
    assert graph_markers["status"] == "done"
    assert {"rss_context_summary", "zotero_context_summary"} <= set(
        graph_markers["artifact_types"]
    )
    assert {"rss_context_agent", "zotero_context_agent"} <= set(
        graph_markers["source_providers"]
    )
    assert graph_markers["stage_statuses"]["chief_of_staff"] == "completed"
    assert graph_markers["stage_statuses"]["opportunity_scout"] == "completed"
    assert output["comparison"]["same_route"] is True
    assert output["comparison"]["same_status"] is False
    assert output["comparison"]["status_improved"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert output["comparison"]["context_evidence_delta"] == 2
    assert output["comparison"]["durable_stage_delta"] >= 3
    assert output["comparison"]["regression_markers"] == []
    assert "status_improved" in output["comparison"]["improvement_markers"]


def test_compare_langgraph_quality_script_runs_outreach_checkpoint_scenario(tmp_path) -> None:
    output = run_comparison(scenario="outreach-checkpoint", database_dir=tmp_path)

    graph_markers = output["graph"]["quality_markers"]
    assert output["scenario"] == "outreach-checkpoint"
    assert output["control"]["quality_markers"]["route"] == "outreach_composer"
    assert graph_markers["route"] == "outreach_composer"
    assert graph_markers["status"] == "blocked"
    assert graph_markers["checkpoint_required"] is True
    assert graph_markers["stage_statuses"]["approval_checkpoint"] == "completed"
    assert graph_markers["stage_statuses"]["outreach_composer"] == "completed"
    assert output["comparison"]["same_route"] is True
    assert output["comparison"]["same_status"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert output["comparison"]["side_effect_safe_both"] is True
    assert output["comparison"]["completed_requested_stage_delta"] == 4
    assert output["comparison"]["regression_markers"] == []
    assert "Completed requested-stage delta: +4" in output["report"]


def test_compare_langgraph_quality_script_json_output(tmp_path, capsys) -> None:
    exit_code = main(["--database-dir", str(tmp_path), "--json", "--require-ready"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["comparison"]["ready_for_live_smoke"] is True
    assert payload["control"]["database_url"].endswith("langgraph_control.sqlite3")
    assert payload["graph"]["database_url"].endswith("langgraph_backend_selected.sqlite3")
    assert payload["live_output_review_rubric"]["minimum_better_than_control"] == [
        "usefulness",
        "relevance",
        "evidence_quality",
        "safety_and_permissions",
    ]
    assert payload["live_output_review_packet"]["decision"] == "unreviewed"
    assert payload["live_open_smoke_checkpoint"]["ready_for_forced_comparison"] is False
    assert payload["live_output_review_decision"]["decision"] == "unreviewed"
    assert payload["live_output_review_decision"]["graph_meets_minimum"] is False
    assert payload["live_smoke_plan"]["schema"] == "keystone.langgraph.live_smoke_plan.v1"
    assert payload["all_scenarios_readiness"]["ready"] is True
    assert payload["live_smoke_plan"]["all_scenarios_ready"] is True
    assert payload["live_smoke_plan"]["all_scenarios_summary"]["scenario_count"] == 7
    assert payload["live_smoke_plan"]["recommended_next_run"] == (
        "open_default_backend_selected_only"
    )


def test_compare_langgraph_quality_require_ready_checks_live_smoke_plan(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    def _not_ready_all_scenarios(*args, **kwargs) -> dict[str, object]:
        return {
            "schema": "keystone.langgraph.all_scenarios_readiness.v1",
            "ready": False,
            "blockers": ["scenario_not_ready:gmail-research-thread-draft"],
            "scenario_count": 7,
            "selected_scenarios": sorted(SCENARIOS),
            "missing_scenarios": [],
            "not_ready_scenarios": ["gmail-research-thread-draft"],
            "regressed_scenarios": [],
            "scenarios": [],
        }

    monkeypatch.setattr(compare_script, "run_all_scenarios", _not_ready_all_scenarios)

    exit_code = main(["--database-dir", str(tmp_path), "--json", "--require-ready"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["comparison"]["ready_for_live_smoke"] is True
    assert payload["live_smoke_plan"]["ready_for_live_smoke"] is False
    assert payload["live_smoke_plan"]["recommended_next_run"] == "do_not_run_live"
    assert "all_scenarios_offline_gate_not_ready" in payload["live_smoke_plan"]["blockers"]


def test_compare_langgraph_quality_require_ready_blocks_dirty_slack_prompt(
    tmp_path,
    capsys,
) -> None:
    exit_code = main(
        [
            "--scenario",
            "gmail-research-thread-draft",
            "--request",
            (
                str(SCENARIOS["gmail-research-thread-draft"]["request_text"])
                + " Compare LangGraph output."
            ),
            "--database-dir",
            str(tmp_path),
            "--json",
            "--require-ready",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 2
    assert payload["comparison"]["ready_for_live_smoke"] is True
    assert payload["live_smoke_plan"]["ready_for_live_smoke"] is False
    assert payload["live_smoke_plan"]["recommended_next_run"] == "do_not_run_live"
    assert payload["live_smoke_plan"]["prompt_cleanliness_blockers"] == [
        "live_smoke_prompt_contains:harness_label"
    ]
    assert "live_smoke_prompt_contains:harness_label" in payload["live_smoke_plan"][
        "blockers"
    ]


def test_compare_langgraph_quality_script_runs_gmail_research_outreach_scenario(
    tmp_path,
) -> None:
    output = run_comparison(scenario="gmail-research-outreach", database_dir=tmp_path)

    graph_markers = output["graph"]["quality_markers"]
    packet = output["live_output_review_packet"]
    assert output["scenario"] == "gmail-research-outreach"
    assert output["control"]["quality_markers"]["route"] == "outreach_composer"
    assert graph_markers["route"] == "outreach_composer"
    assert graph_markers["status"] == "needs_approval"
    assert graph_markers["checkpoint_required"] is True
    assert graph_markers["stage_statuses"]["gmail_triage"] == "completed"
    assert graph_markers["stage_statuses"]["business_research"] == "completed"
    assert graph_markers["stage_statuses"]["outreach_composer"] == "completed"
    assert output["comparison"]["same_route"] is True
    assert output["comparison"]["same_status"] is True
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert output["comparison"]["regression_markers"] == []
    assert packet["schema"] == "keystone.langgraph.live_output_review_packet.v1"
    assert packet["decision"] == "unreviewed"
    assert [item["mode"] for item in packet["mode_evidence"]] == [
        "open_default_backend_selected",
        "forced_langgraph_false_control",
        "forced_langgraph_true",
    ]
    assert [item["expected_environment"] for item in packet["mode_evidence"]] == [
        {"KEYSTONE_WORKITEM_LANGGRAPH": "unset"},
        {"KEYSTONE_WORKITEM_LANGGRAPH": "false"},
        {"KEYSTONE_WORKITEM_LANGGRAPH": "true"},
    ]
    assert packet["graph_output"]["artifact_summary_count"] >= 2
    assert packet["minimum_better_than_control"] == [
        "usefulness",
        "relevance",
        "evidence_quality",
        "safety_and_permissions",
    ]
    assert output["live_smoke_plan"]["ready_for_live_smoke"] is False
    assert output["all_scenarios_readiness"] is None
    assert output["live_smoke_plan"]["all_scenarios_ready"] is None
    assert output["live_smoke_plan"]["recommended_next_run"] == "do_not_run_live"
    assert "all_scenarios_offline_gate_not_ready" in output["live_smoke_plan"]["blockers"]
    assert output["live_smoke_plan"]["live_controls"]["live_search"] is False
    assert output["live_smoke_plan"]["live_controls"]["external_writes_enabled"] is False
    assert output["live_smoke_plan"]["max_live_workflow_runs_before_review"] == 1
    assert output["live_smoke_plan"]["output_quality_success_criteria"] == [
        "visible answer gives a clearer operator decision or next action than control",
        "visible answer is specific to the requested company/topic and does not drift to generic leads",
        "visible answer preserves source-backed claims, evidence gaps, and uncertainty",
        "visible answer includes the requested draft/work product when safety gates allow it",
        "visible answer avoids route/debug metadata as the main substance",
        "visible answer does not add verbosity or API calls without user-facing value",
    ]


def test_compare_langgraph_quality_script_runs_gmail_thread_draft_scenario(
    tmp_path,
) -> None:
    output = run_comparison(scenario="gmail-research-thread-draft", database_dir=tmp_path)

    graph_markers = output["graph"]["quality_markers"]
    packet = output["live_output_review_packet"]
    assert output["scenario"] == "gmail-research-thread-draft"
    assert output["control"]["quality_markers"]["route"] == "outreach_composer"
    assert output["control"]["quality_markers"]["status"] == "done"
    assert graph_markers["route"] == "outreach_composer"
    assert graph_markers["status"] == "done"
    assert graph_markers["stage_statuses"]["gmail_triage"] == "completed"
    assert graph_markers["stage_statuses"]["business_research"] == "completed"
    assert graph_markers["stage_statuses"]["outreach_composer"] == "completed"
    assert "outreach_draft" in graph_markers["artifact_types"]
    assert output["comparison"]["same_route"] is True
    assert output["comparison"]["same_status"] is True
    assert output["comparison"]["status_improved"] is False
    assert output["comparison"]["ready_for_live_smoke"] is True
    assert output["comparison"]["regression_markers"] == []
    assert packet["decision"] == "unreviewed"
    assert packet["graph_output"]["blockers"] == []
    assert packet["graph_output"]["status"] == "done"
    assert "Draft-only" in packet["graph_output"]["human_summary"]
    assert output["live_smoke_plan"]["slack_prompt_template"].startswith("@KNI gmail triage")
    assert "Do not send" not in output["live_smoke_plan"]["slack_prompt_template"]
    assert "Live SDK is approved" not in output["live_smoke_plan"]["slack_prompt_template"]
    assert "live web search is not approved" not in output["live_smoke_plan"][
        "slack_prompt_template"
    ]
    assert output["live_smoke_plan"]["live_controls"]["live_search"] is False
    assert output["live_smoke_plan"]["live_controls"]["external_writes_enabled"] is False
    assert output["live_smoke_plan"]["prompt_cleanliness_blockers"] == []
    assert "open/default" not in output["live_smoke_plan"]["slack_prompt_template"]
    assert "forced true" not in output["live_smoke_plan"]["slack_prompt_template"]
    assert "LangGraph" not in output["live_smoke_plan"]["slack_prompt_template"]


def test_live_smoke_plan_strips_harness_disclaimers_from_slack_prompt() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-thread-draft",
        request_text=(
            "@KNI business agents open/default LangGraph smoke: gmail triage this "
            "sanitized inbound email from Mindful Care, research Mindful Care, "
            "and prepare a draft-only Slack-thread sample outreach for review. "
            "Live SDK is approved only for this bounded read-only smoke if the "
            "backend would normally use it. live web search is not approved. "
            "Use local/dry-run retrieval where possible. Do not send, create "
            "Gmail drafts, post outside this thread, schedule, publish, create "
            "files, update Airtable/CRM/Drive/Sheets, or write external systems. "
            "Also keep track of this run costs."
        ),
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    prompt = plan["slack_prompt_template"]

    assert prompt == (
        "@KNI gmail triage this sanitized inbound email from Mindful Care, "
        "research Mindful Care, and prepare a draft-only Slack-thread sample "
        "outreach for review."
    )
    assert prompt.count("@KNI") == 1
    assert "open/default" not in prompt
    assert "LangGraph" not in prompt
    assert "Live SDK is approved" not in prompt
    assert "live web search is not approved" not in prompt
    assert "Do not send" not in prompt
    assert "run costs" not in prompt
    assert plan["prompt_cleanliness_blockers"] == []


def test_live_smoke_plan_blocks_when_sanitized_prompt_still_contains_harness_text() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-thread-draft",
        request_text="compare LangGraph orchestration output for this smoke run",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert plan["prompt_cleanliness_blockers"] == [
        "live_smoke_prompt_contains:harness_label"
    ]
    assert "live_smoke_prompt_contains:harness_label" in plan["blockers"]


def test_live_smoke_plan_renderer_shows_prompt_cleanliness_blockers() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-thread-draft",
        request_text="compare LangGraph output",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    report = render_langgraph_live_smoke_plan(plan)

    assert "- Ready for live smoke: no" in report
    assert (
        "- Prompt cleanliness blockers: live_smoke_prompt_contains:harness_label"
        in report
    )
    assert "- Blockers: live_smoke_prompt_contains:harness_label" in report


def test_compare_langgraph_quality_script_can_print_live_output_rubric(
    tmp_path,
    capsys,
) -> None:
    exit_code = main(
        [
            "--database-dir",
            str(tmp_path),
            "--scenario",
            "outreach-checkpoint",
            "--include-output-rubric",
            "--include-live-smoke-plan",
            "--require-ready",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Live output review rubric" in captured.out
    assert "Live output usefulness comparison" in captured.out
    assert "Live output review decision" in captured.out
    assert "Bounded live-smoke plan" in captured.out
    assert "Open/default live smoke checkpoint" in captured.out
    assert "usefulness:" in captured.out
    assert "evidence_quality:" in captured.out
    assert "Mode open_default_backend_selected: KEYSTONE_WORKITEM_LANGGRAPH=unset" in (
        captured.out
    )


def test_live_output_review_rubric_names_human_value_dimensions() -> None:
    report = render_langgraph_live_output_review_rubric()

    assert "usefulness" in report
    assert "detail" in report
    assert "relevance" in report
    assert "safety_and_permissions" in report


def test_live_output_review_packet_renders_unreviewed_decision() -> None:
    report = render_langgraph_live_output_review_packet(
        {
            "minimum_better_than_control": ["usefulness", "relevance"],
            "control_output": {"output_char_count": 12, "artifact_summary_count": 1},
            "graph_output": {"output_char_count": 24, "artifact_summary_count": 2},
        }
    )

    assert "unreviewed" in report
    assert "All-scenarios offline gate: no" in report
    assert "Control output chars: 12" in report
    assert "Graph artifact summaries: 2" in report
    assert "source URLs" in report
    assert "langgraph_orchestration_event" in report


def test_finalize_live_output_review_requires_graph_to_win_minimum_criteria() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)

    decision = finalize_langgraph_live_output_review(packet)
    report = render_langgraph_live_output_review_decision(decision)

    assert decision["schema"] == "keystone.langgraph.live_output_review_decision.v1"
    assert decision["decision"] == "graph_better"
    assert decision["graph_meets_minimum"] is True
    assert decision["minimum_control_wins"] == []
    assert decision["blockers"] == []
    assert decision["live_sdk_request_count"] == 2
    assert decision["approved_max_live_sdk_calls"] == 5
    assert "Decision: graph_better" in report


def test_finalize_live_output_review_marks_control_better_on_minimum_loss() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_required_mode_evidence(packet)
    for question in packet["review_questions"]:
        question["winner"] = "graph"
        question["control_observation"] = f"{question['criterion']} control observation"
        question["graph_observation"] = f"{question['criterion']} graph observation"
        if question["criterion"] == "relevance":
            question["winner"] = "control"

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "control_better"
    assert decision["graph_meets_minimum"] is False
    assert decision["minimum_control_wins"] == ["relevance"]


def test_finalize_live_output_review_blocks_positive_decision_without_review_observations() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    for question in packet["review_questions"]:
        question["winner"] = "graph"
        if question["criterion"] == "efficiency":
            question["winner"] = "tie"
    _fill_required_mode_evidence(packet)

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "usefulness.control_observation" in decision["missing_observations"]
    assert "missing_review_observation:usefulness.graph_observation" in (
        decision["blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_without_run_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.work_item_id" in (
        decision["evidence_blockers"]
    )
    assert "missing_mode_evidence:forced_langgraph_false_control.visible_output" in (
        decision["blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_without_slack_display_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["slack_display_title"] = ""

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.slack_display_title" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_without_graph_event() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["langgraph_orchestration_event"] = {}

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.langgraph_orchestration_event" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_without_forced_environment() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][1]["actual_environment"] = {}

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_false_control.actual_environment" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_with_wrong_forced_environment() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["actual_environment"] = {
        "KEYSTONE_WORKITEM_LANGGRAPH": "false"
    }

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert (
        "invalid_mode_evidence:forced_langgraph_true.actual_environment.KEYSTONE_WORKITEM_LANGGRAPH"
        in decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_forced_off_graph_event_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][1]["langgraph_orchestration_event"] = {
        "schema": "keystone.langgraph.orchestration.v1",
        "node_path": ["chief_of_staff", "business_research"],
    }

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "unexpected_mode_evidence:forced_langgraph_false_control.langgraph_orchestration_event" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_invalid_graph_event_schema() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["langgraph_orchestration_event"] = {
        "schema": "example.invalid",
        "node_path": ["chief_of_staff", "business_research"],
    }

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "invalid_mode_evidence:forced_langgraph_true.langgraph_orchestration_event.schema" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_without_source_urls() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["source_urls"] = []

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.source_urls" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_with_side_effect() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["side_effects"] = {
        **NO_SIDE_EFFECTS,
        "gmail_draft_created": True,
    }

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "side_effect_detected:forced_langgraph_true.gmail_draft_created" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_with_partial_side_effects() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["side_effects"] = {"send_attempted": False}

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.side_effects.gmail_draft_created" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_requires_explicit_side_effect_review() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["side_effects_reviewed"] = False

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "missing_mode_evidence:forced_langgraph_true.side_effects_reviewed" in (
        decision["evidence_blockers"]
    )


def test_finalize_live_output_review_blocks_positive_decision_with_weak_usage_event() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["workflow_sdk_usage_event"] = {
        "schema": "keystone.workflow_sdk_usage.v1",
        "usage": {"requests": 1},
        "cost": {"estimated_usd": 0.001},
    }

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert (
        "missing_mode_evidence:forced_langgraph_true.workflow_sdk_usage_event.request_cache"
        in decision["evidence_blockers"]
    )


def test_finalize_live_output_review_requires_all_scenarios_gate() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["all_scenarios_ready"] = False

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert "all_scenarios_offline_gate_not_ready" in decision["evidence_blockers"]


def test_finalize_live_output_review_blocks_positive_decision_over_internal_request_threshold() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_open_default_mode_evidence(packet)
    _fill_required_mode_evidence(packet)
    for item in packet["mode_evidence"]:
        if item["mode"] != "forced_langgraph_true":
            continue
        usage_event = item.get("workflow_sdk_usage_event")
        if isinstance(usage_event, dict):
            usage_event["usage"]["requests"] = 5

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert decision["graph_meets_minimum"] is False
    assert decision["live_sdk_request_count"] == 7
    assert decision["approved_max_live_sdk_calls"] == 5
    assert decision["approved_live_api_test_budget"] == 5
    assert decision["internal_sdk_request_review_threshold_per_run"] == 4
    assert (
        "internal_sdk_request_threshold_exceeded:forced_langgraph_true:5>4"
        in decision["evidence_blockers"]
    )


def test_finalize_live_output_review_counts_plural_sdk_usage_events() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_open_default_mode_evidence(packet)
    _fill_required_mode_evidence(packet)
    for item in packet["mode_evidence"]:
        usage_event = item.get("workflow_sdk_usage_event")
        if not isinstance(usage_event, dict):
            continue
        first = dict(usage_event)
        second = {
            "schema": "keystone.workflow_sdk_usage.v1",
            "usage": {"requests": 1},
            "cost": {"estimated_usd": 0.001},
            "request_cache": {
                "static_prefix_sha256": f"{item['mode']}-second-static",
            },
        }
        item["workflow_sdk_usage_events"] = [first, second]

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "graph_better"
    assert decision["graph_meets_minimum"] is True
    assert decision["live_sdk_request_count"] == 6
    assert decision["internal_sdk_request_threshold_blockers"] == []


def test_open_smoke_checkpoint_blocks_forced_runs_until_open_evidence_is_present() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]

    checkpoint = langgraph_open_smoke_checkpoint(packet)
    report = render_langgraph_open_smoke_checkpoint(checkpoint)

    assert checkpoint["schema"] == "keystone.langgraph.live_open_smoke_checkpoint.v1"
    assert checkpoint["ready_for_forced_comparison"] is False
    assert checkpoint["requires_next_step"] == "inspect_or_fix_open_default_run"
    assert "missing_mode_evidence:open_default_backend_selected.work_item_id" in (
        checkpoint["blockers"]
    )
    assert "Ready for forced comparison: no" in report


def test_open_smoke_checkpoint_allows_forced_runs_after_open_evidence_is_present() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)

    checkpoint = langgraph_open_smoke_checkpoint(packet)
    report = render_langgraph_open_smoke_checkpoint(checkpoint)

    assert checkpoint["ready_for_forced_comparison"] is True
    assert checkpoint["requires_next_step"] == "run_forced_langgraph_false_control"
    assert checkpoint["route"] == "outreach_composer"
    assert checkpoint["status"] == "done"
    assert checkpoint["live_sdk_request_count"] == 1
    assert checkpoint["live_api_test_attempt_count"] == 1
    assert checkpoint["remaining_live_sdk_calls"] == 4
    assert checkpoint["remaining_live_api_test_attempts"] == 4
    assert checkpoint["forced_comparison_min_live_sdk_calls"] == 2
    assert checkpoint["internal_sdk_request_review_threshold_per_run"] == 4
    assert checkpoint["blockers"] == []
    assert "- Internal SDK requests observed: 1" in report
    assert "- Internal SDK request threshold/run: 4" in report
    assert "- Live API test attempts used: 1" in report
    assert "- Live API test attempts remaining: 4" in report


def test_open_smoke_checkpoint_allows_backend_selected_graph_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["langgraph_orchestration_event"] = {
        "schema": "keystone.langgraph.orchestration.v1",
        "node_path": ["gmail_triage", "business_research", "outreach_composer"],
    }

    checkpoint = langgraph_open_smoke_checkpoint(packet)

    assert checkpoint["ready_for_forced_comparison"] is True
    assert checkpoint["blockers"] == []


def test_open_smoke_checkpoint_allows_legacy_backend_selected_graph_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["langgraph_orchestration_event"] = {
        "runtime": "langgraph",
        "node_path": ["gmail_triage", "business_research", "outreach_composer"],
    }

    checkpoint = langgraph_open_smoke_checkpoint(packet)

    assert checkpoint["ready_for_forced_comparison"] is True
    assert checkpoint["blockers"] == []


def test_open_smoke_checkpoint_blocks_invalid_backend_selected_graph_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["langgraph_orchestration_event"] = {
        "schema": "example.invalid",
        "node_path": ["gmail_triage", "business_research", "outreach_composer"],
    }

    checkpoint = langgraph_open_smoke_checkpoint(packet)

    assert checkpoint["ready_for_forced_comparison"] is False
    assert (
        "invalid_mode_evidence:open_default_backend_selected.langgraph_orchestration_event.schema"
        in checkpoint["blockers"]
    )


def test_open_smoke_checkpoint_blocks_forced_runs_when_internal_request_threshold_exceeded() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["workflow_sdk_usage_event"]["usage"]["requests"] = 5

    checkpoint = langgraph_open_smoke_checkpoint(packet)
    report = render_langgraph_open_smoke_checkpoint(checkpoint)

    assert checkpoint["ready_for_forced_comparison"] is False
    assert checkpoint["requires_next_step"] == "inspect_or_fix_open_default_run"
    assert checkpoint["live_sdk_request_count"] == 5
    assert checkpoint["live_api_test_attempt_count"] == 1
    assert checkpoint["remaining_live_api_test_attempts"] == 4
    assert (
        "internal_sdk_request_threshold_exceeded:open_default_backend_selected:5>4"
        in checkpoint["blockers"]
    )
    assert "- Internal SDK requests observed: 5" in report
    assert "- Live API test attempts remaining: 4" in report


def test_open_smoke_checkpoint_blocks_forced_runs_without_slack_display_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["operator_status"] = ""

    checkpoint = langgraph_open_smoke_checkpoint(packet)

    assert checkpoint["ready_for_forced_comparison"] is False
    assert (
        "missing_mode_evidence:open_default_backend_selected.operator_status"
        in checkpoint["blockers"]
    )


def test_open_smoke_checkpoint_rejects_failed_or_side_effect_output() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["status"] = "failed"
    packet["mode_evidence"][0]["visible_output"] = "Created Gmail draft for the recipient."

    checkpoint = langgraph_open_smoke_checkpoint(packet)

    assert checkpoint["ready_for_forced_comparison"] is False
    assert "open_default_status_not_done:failed" in checkpoint["blockers"]
    assert "open_default_visible_output_implies_external_side_effect" in (
        checkpoint["blockers"]
    )


def test_open_smoke_checkpoint_rejects_blocked_output_without_needs_input_display() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet["mode_evidence"][0]["status"] = "blocked"
    packet["mode_evidence"][0]["operator_status"] = ""
    packet["mode_evidence"][0]["slack_display_title"] = ""
    packet["mode_evidence"][0]["visible_output"] = "Business Agents WorkItem Blocked."

    checkpoint = langgraph_open_smoke_checkpoint(packet)
    report = render_langgraph_open_smoke_checkpoint(checkpoint)

    assert checkpoint["ready_for_forced_comparison"] is False
    assert "open_default_status_not_done:blocked" in checkpoint["blockers"]
    assert "- Operator status: n/a" in report
    assert "- Slack display title: n/a" in report
    assert (
        "blocked_mode_without_needs_input_operator_status:open_default_backend_selected"
        in checkpoint["blockers"]
    )
    assert (
        "blocked_mode_missing_slack_display_title:open_default_backend_selected"
        in checkpoint["blockers"]
    )
    assert (
        "blocked_mode_visible_output_lacks_next_input_request:open_default_backend_selected"
        in checkpoint["blockers"]
    )


def test_live_output_evidence_allows_blocked_mode_with_needs_input_display() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    for item in packet["mode_evidence"]:
        if item["mode"] != "forced_langgraph_true":
            continue
        item.update(
            {
                "status": "blocked",
                "operator_status": "needs_input",
                "slack_display_title": "Business Agents Need Input",
                "visible_output": (
                    "What should this outreach focus on? Reply with the target, "
                    "focus, and approved source-backed facts."
                ),
            }
        )

    decision = finalize_langgraph_live_output_review(packet)

    assert not any(
        blocker.startswith("blocked_mode_") for blocker in decision["evidence_blockers"]
    )


def test_finalize_live_output_review_stays_unreviewed_until_all_criteria_scored() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    for question in packet["review_questions"]:
        if question["criterion"] == "usefulness":
            question["winner"] = "graph"

    decision = finalize_langgraph_live_output_review(packet)

    assert decision["decision"] == "unreviewed"
    assert "unreviewed:relevance" in decision["blockers"]
    assert "unreviewed:evidence_quality" in decision["blockers"]


def test_live_visible_output_scorecard_waits_for_forced_mode_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")[
        "live_output_review_packet"
    ]

    scored = langgraph_live_user_facing_output_quality(packet)

    assert scored["status"] == "pending_live_mode_evidence"
    assert "control.visible_output" in scored["missing"]
    assert "graph.workflow_sdk_usage_event" in scored["missing"]


def test_live_visible_output_scorecard_scores_complete_matched_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")[
        "live_output_review_packet"
    ]
    packet["quality_target_terms"] = ["Mindful Care", "evaluation design"]
    _fill_required_mode_evidence(packet)
    control = next(
        item
        for item in packet["mode_evidence"]
        if item["mode"] == "forced_langgraph_false_control"
    )
    graph = next(
        item
        for item in packet["mode_evidence"]
        if item["mode"] == "forced_langgraph_true"
    )
    control["visible_output"] = (
        "Mindful Care asked about evaluation design. Draft reply for review only."
    )
    graph["visible_output"] = (
        "*Recommendation:*\nProceed after human review.\n\n"
        "*Strongest evidence:*\n- Mindful Care asked about evaluation design.\n"
        "- The supplied thread requests measurement support.\n\n"
        "*Main uncertainty:*\n- The exact workflow is unknown.\n\n"
        "*Next step:*\nReview the reply and ask for workflow context.\n\n"
        "*Draft for review:*\nWould a brief conversation be useful?"
    )
    for item in (control, graph):
        item["workflow_sdk_usage_event"]["usage"].update(
            {"input_tokens": 37000, "output_tokens": 400, "total_tokens": 37400}
        )

    scored = langgraph_live_user_facing_output_quality(packet)
    decision = finalize_langgraph_live_output_review(packet)

    assert scored["status"] == "scored"
    assert scored["graph"]["passed"] is True
    assert scored["comparison"]["graph_quality_improved"] is True
    assert decision["user_facing_output_quality"] == scored
    rendered = render_langgraph_live_output_review_decision(decision)
    assert "Automated visible-output score: scored" in rendered
    assert "Automated graph quality improved: yes" in rendered


def test_live_visible_output_scorecard_surfaces_safety_and_metadata_regressions() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")[
        "live_output_review_packet"
    ]
    packet["quality_target_terms"] = ["Mindful Care"]
    _fill_required_mode_evidence(packet)
    graph = next(
        item
        for item in packet["mode_evidence"]
        if item["mode"] == "forced_langgraph_true"
    )
    graph["visible_output"] = (
        "*Recommendation:*\nReview Mindful Care.\n\n"
        "*Strongest evidence:*\n- Evidence one.\n- Evidence two.\n\n"
        "*Main uncertainty:*\n- Unknown.\n\n"
        "*Next step:*\nReview the WorkItem node_path.\n\n"
        "*Draft for review:*\nDraft."
    )
    graph["side_effects"] = {
        **graph["side_effects"],
        "external_write_performed": True,
    }

    scored = langgraph_live_user_facing_output_quality(packet)

    assert scored["graph"]["dimensions"]["side_effect_safety"] == 0
    assert "workitem" in scored["graph"]["diagnostics"]["backend_terms"]
    assert "node_path" in scored["graph"]["diagnostics"]["backend_terms"]
    assert scored["comparison"]["graph_quality_improved"] is False


def test_compare_script_finalizes_edited_review_packet_json(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    exit_code = main(["--review-packet", str(packet_path), "--require-graph-better"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Decision: graph_better" in captured.out


def test_compare_script_rejects_graph_positive_review_without_graph_event(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _mark_review_packet_graph_better(packet)
    _fill_required_mode_evidence(packet)
    packet["mode_evidence"][2]["langgraph_orchestration_event"] = {}
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    exit_code = main(["--review-packet", str(packet_path), "--require-graph-better"])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "missing_mode_evidence:forced_langgraph_true.langgraph_orchestration_event" in (
        captured.out
    )


def test_compare_script_writes_review_packet_for_live_evidence_capture(
    tmp_path,
    capsys,
) -> None:
    packet_path = tmp_path / "artifacts" / "langgraph-live-review-packet.json"

    exit_code = main(
        [
            "--scenario",
            "gmail-research-thread-draft",
            "--write-review-packet",
            str(packet_path),
            "--require-ready",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(packet_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert packet_path.exists()
    assert "Wrote live-output review packet:" in captured.out
    assert payload["schema"] == "keystone.langgraph.live_output_review_packet.v1"
    assert payload["decision"] == "unreviewed"
    assert payload["all_scenarios_ready"] is True
    assert payload["all_scenarios_summary"]["scenario_count"] == 7
    assert payload["mode_evidence"][0]["mode"] == "open_default_backend_selected"
    assert payload["mode_evidence"][0]["slack_permalink"] == ""
    assert payload["mode_evidence"][0]["operator_status"] == ""
    assert payload["mode_evidence"][0]["slack_display_title"] == ""
    assert payload["mode_evidence"][1]["expected_environment"] == {
        "KEYSTONE_WORKITEM_LANGGRAPH": "false"
    }


def test_compare_script_merges_captured_slack_evidence_into_review_packet(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "open-evidence.json"
    merged_path = tmp_path / "merged-review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "slack_permalink": "https://example.slack.com/open-default",
                "slack_message_ts": "1783194986.634339",
                "result": {
                    "route": "outreach_composer",
                    "status": "done",
                    "operator_status": "completed",
                    "slack_display_title": "Business Agents Run Completed",
                    "human_summary": (
                        "Draft-only Slack-thread sample outreach for review. "
                        "No external message was sent, no Gmail draft was created, "
                        "and no external write was performed."
                    ),
                    "source_urls": ["https://example.com/mindful-care-source"],
                    "side_effects": NO_SIDE_EFFECTS,
                    "side_effects_reviewed": True,
                    "workflow_sdk_usage_event": {
                        "schema": "keystone.workflow_sdk_usage.v1",
                        "usage": {"requests": 1},
                        "cost": {"estimated_usd": 0.001},
                        "request_cache": {
                            "static_prefix_sha256": "open-static",
                            "dynamic_prompt_sha256": "open-dynamic",
                        },
                    },
                    "slack_run_provenance": {
                        "work_item_id": "wi_open_default",
                        "source_thread_ts": "1783194986.634339",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "open_default_backend_selected",
            "--evidence-json",
            str(evidence_path),
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    open_evidence = merged["mode_evidence"][0]

    assert exit_code == 0
    assert open_evidence["slack_permalink"] == "https://example.slack.com/open-default"
    assert open_evidence["work_item_id"] == "wi_open_default"
    assert open_evidence["route"] == "outreach_composer"
    assert open_evidence["status"] == "done"
    assert open_evidence["operator_status"] == "completed"
    assert open_evidence["slack_display_title"] == "Business Agents Run Completed"
    assert open_evidence["source_urls"] == ["https://example.com/mindful-care-source"]
    assert open_evidence["side_effects"] == NO_SIDE_EFFECTS
    assert open_evidence["side_effects_reviewed"] is True
    assert open_evidence["workflow_sdk_usage_event"]["usage"]["requests"] == 1
    assert open_evidence["workflow_sdk_usage_events"][0]["usage"]["requests"] == 1
    assert "No external message was sent" in open_evidence["visible_output"]
    assert output["live_open_smoke_checkpoint"]["ready_for_forced_comparison"] is True


def test_compare_script_merge_applies_actual_environment_override(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "forced-off-evidence.json"
    merged_path = tmp_path / "merged-review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "mode": "forced_langgraph_false_control",
                "slack_permalink": "https://example.slack.com/forced-off",
                "result": {
                    "route": "outreach_composer",
                    "status": "done",
                    "operator_status": "completed",
                    "slack_display_title": "Business Agents Draft Ready",
                    "human_summary": "Draft-only Slack-thread sample outreach.",
                    "source_urls": ["https://example.com/mindful-care-source"],
                    "side_effects": NO_SIDE_EFFECTS,
                    "side_effects_reviewed": True,
                    "workflow_sdk_usage_event": {
                        "schema": "keystone.workflow_sdk_usage.v1",
                        "usage": {"requests": 1},
                        "cost": {"estimated_usd": 0.001},
                        "request_cache": {
                            "static_prefix_sha256": "forced-off-static",
                            "dynamic_prompt_sha256": "forced-off-dynamic",
                        },
                    },
                    "slack_run_provenance": {
                        "work_item_id": "wi_forced_off",
                        "source_thread_ts": "1783194986.634339",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "forced_langgraph_false_control",
            "--evidence-json",
            str(evidence_path),
            "--actual-environment",
            "KEYSTONE_WORKITEM_LANGGRAPH=false",
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    capsys.readouterr()
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    forced_off = merged["mode_evidence"][1]

    assert exit_code == 0
    assert forced_off["work_item_id"] == "wi_forced_off"
    assert forced_off["actual_environment"] == {"KEYSTONE_WORKITEM_LANGGRAPH": "false"}


def test_compare_script_merges_all_workflow_usage_events_from_captured_events(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "open-default-events.json"
    merged_path = tmp_path / "merged-review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "mode": "open_default_backend_selected",
                "slack_permalink": "https://example.slack.com/open-default",
                "result": {
                    "route": "outreach_composer",
                    "status": "done",
                    "operator_status": "completed",
                    "slack_display_title": "Business Agents Run Completed",
                    "human_summary": "Draft-only output with no external write.",
                    "source_urls": ["https://example.com/mindful-care-source"],
                    "side_effects": NO_SIDE_EFFECTS,
                    "side_effects_reviewed": True,
                    "slack_run_provenance": {"work_item_id": "wi_open_default"},
                },
                "events": [
                    {
                        "event_type": "workflow_sdk_usage",
                        "metadata": {
                            "schema": "keystone.workflow_sdk_usage.v1",
                            "agent": "manual_request_planner",
                            "usage": {"requests": 1},
                            "cost": {"estimated_usd": 0.001},
                            "request_cache": {"static_prefix_sha256": "planner-static"},
                        },
                    },
                    {
                        "event_type": "workflow_sdk_usage",
                        "metadata": {
                            "schema": "keystone.workflow_sdk_usage.v1",
                            "agent": "outreach_composer",
                            "usage": {"requests": 1},
                            "cost": {"estimated_usd": 0.002},
                            "request_cache": {"static_prefix_sha256": "outreach-static"},
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "open_default_backend_selected",
            "--evidence-json",
            str(evidence_path),
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    open_evidence = merged["mode_evidence"][0]

    assert exit_code == 0
    assert [event["agent"] for event in open_evidence["workflow_sdk_usage_events"]] == [
        "manual_request_planner",
        "outreach_composer",
    ]
    assert open_evidence["workflow_sdk_usage_event"]["agent"] == "outreach_composer"


def test_compare_script_merges_graph_orchestration_event_from_captured_events(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "forced-true-evidence.json"
    merged_path = tmp_path / "merged-review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "mode": "forced_langgraph_true",
                "slack_permalink": "https://example.slack.com/forced-true",
                "result": {
                    "route": "outreach_composer",
                    "status": "done",
                    "operator_status": "completed",
                    "slack_display_title": "Business Agents Run Completed",
                    "human_summary": "Graph run completed with no external write.",
                    "source_urls": ["https://example.com/forced-true-source"],
                    "side_effects": NO_SIDE_EFFECTS,
                    "side_effects_reviewed": True,
                    "workflow_sdk_usage_event": {
                        "schema": "keystone.workflow_sdk_usage.v1",
                        "usage": {"requests": 1},
                        "cost": {"estimated_usd": 0.001},
                        "request_cache": {
                            "static_prefix_sha256": "forced-static",
                            "dynamic_prompt_sha256": "forced-dynamic",
                        },
                    },
                    "slack_run_provenance": {"work_item_id": "wi_forced_true"},
                },
                "events": [
                    {
                        "event_type": "langgraph_orchestration",
                        "metadata": {
                            "schema": "keystone.langgraph.orchestration.v1",
                            "node_path": [
                                "chief_of_staff",
                                "business_research",
                                "outreach_composer",
                            ],
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "forced_langgraph_true",
            "--evidence-json",
            str(evidence_path),
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    forced_true = merged["mode_evidence"][2]

    assert exit_code == 0
    assert forced_true["work_item_id"] == "wi_forced_true"
    assert forced_true["langgraph_orchestration_event"]["schema"] == (
        "keystone.langgraph.orchestration.v1"
    )
    assert "outreach_composer" in forced_true["langgraph_orchestration_event"]["node_path"]
    assert forced_true["source_urls"] == ["https://example.com/forced-true-source"]
    assert forced_true["side_effects"] == NO_SIDE_EFFECTS
    assert forced_true["side_effects_reviewed"] is True


def test_compare_script_writes_live_slack_evidence_template(tmp_path, capsys) -> None:
    evidence_path = tmp_path / "open-default-evidence.json"

    exit_code = main(
        [
            "--write-evidence-template",
            str(evidence_path),
            "--evidence-mode",
            "open_default_backend_selected",
        ]
    )

    captured = capsys.readouterr()
    template = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "Wrote live Slack evidence template:" in captured.out
    assert template["schema"] == "keystone.langgraph.live_slack_evidence.v1"
    assert template["mode"] == "open_default_backend_selected"
    assert template["partial_db_extraction"] is False
    assert template["actual_environment"] == {}
    assert template["operator_status"] == ""
    assert template["slack_display_title"] == ""
    assert template["source_urls"] == []
    assert template["side_effects"] == NO_SIDE_EFFECTS
    assert template["side_effects_reviewed"] is False
    assert template["workflow_sdk_usage_event"]["schema"] == "keystone.workflow_sdk_usage.v1"
    assert template["workflow_sdk_usage_events"] == []
    assert template["langgraph_orchestration_event"] == {}
    assert any("Do not invent usage" in item for item in template["capture_instructions"])
    assert any("side_effects_reviewed=true" in item for item in template["capture_instructions"])
    assert any("langgraph_orchestration event" in item for item in template["capture_instructions"])
    assert any("actual_environment.KEYSTONE_WORKITEM_LANGGRAPH" in item for item in template["capture_instructions"])


def test_compare_script_writes_actual_environment_to_evidence_template(
    tmp_path,
    capsys,
) -> None:
    evidence_path = tmp_path / "forced-off-evidence.json"

    exit_code = main(
        [
            "--write-evidence-template",
            str(evidence_path),
            "--evidence-mode",
            "forced_langgraph_false_control",
            "--actual-environment",
            "KEYSTONE_WORKITEM_LANGGRAPH=false",
        ]
    )

    captured = capsys.readouterr()
    template = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "Wrote live Slack evidence template:" in captured.out
    assert template["actual_environment"] == {"KEYSTONE_WORKITEM_LANGGRAPH": "false"}


def test_compare_script_rejects_invalid_actual_environment_argument(tmp_path) -> None:
    evidence_path = tmp_path / "forced-off-evidence.json"

    with pytest.raises(ValueError, match="--actual-environment values must use KEY=VALUE"):
        main(
            [
                "--write-evidence-template",
                str(evidence_path),
                "--evidence-mode",
                "forced_langgraph_false_control",
                "--actual-environment",
                "KEYSTONE_WORKITEM_LANGGRAPH",
            ]
        )


def test_compare_script_extracts_partial_live_evidence_from_work_item_db(
    tmp_path,
    capsys,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_live_capture",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        request_text="gmail triage this sanitized inbound email",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        sources=[
            WorkItemSourceRef(
                title="Mindful Care source",
                url="https://example.com/mindful-care-source",
            )
        ],
        artifact_refs=[
            WorkItemArtifactRef(
                artifact_type="outreach_draft",
                artifact_id="draft_1",
                metadata={
                    "gmail_draft_created": False,
                    "external_write_performed": False,
                },
            )
        ],
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="manual_request_planner",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "manual_request_planner",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.001},
                "request_cache": {"static_prefix_sha256": "planner-static"},
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )
    evidence_path = tmp_path / "db-evidence.json"

    exit_code = main(
        [
            "--evidence-from-work-item",
            work_item.id,
            "--evidence-database-url",
            db_url,
            "--evidence-mode",
            "forced_langgraph_true",
            "--write-evidence-template",
            str(evidence_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert output["work_item_id"] == work_item.id
    assert evidence["mode"] == "forced_langgraph_true"
    assert evidence["partial_db_extraction"] is True
    assert evidence["work_item_id"] == work_item.id
    assert evidence["route"] == "outreach_composer"
    assert evidence["status"] == "done"
    assert evidence["source_urls"] == ["https://example.com/mindful-care-source"]
    assert [event["agent"] for event in evidence["workflow_sdk_usage_events"]] == [
        "manual_request_planner",
        "outreach_composer",
    ]
    assert evidence["workflow_sdk_usage_event"]["agent"] == "outreach_composer"
    assert evidence["langgraph_orchestration_event"]["schema"] == (
        "keystone.langgraph.orchestration.v1"
    )
    assert evidence["side_effects"] == NO_SIDE_EFFECTS
    assert evidence["side_effects_reviewed"] is False
    assert evidence["slack_permalink"] == ""
    assert evidence["visible_output"] == ""
    assert "Partial DB extraction" in evidence["notes"]


def test_compare_script_rejects_forced_off_db_evidence_template_with_graph_event(
    tmp_path,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_forced_off_template_graph_event",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        request_text="gmail triage this sanitized inbound email",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )

    with pytest.raises(ValueError, match="must not include langgraph_orchestration_event"):
        main(
            [
                "--evidence-from-work-item",
                work_item.id,
                "--evidence-database-url",
                db_url,
                "--evidence-mode",
                "forced_langgraph_false_control",
                "--json",
            ]
        )


def test_compare_script_rejects_forced_true_db_evidence_template_without_graph_event(
    tmp_path,
) -> None:
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_forced_true_template_missing_graph_event",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        request_text="gmail triage this sanitized inbound email",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )

    with pytest.raises(ValueError, match="must include langgraph_orchestration_event"):
        main(
            [
                "--evidence-from-work-item",
                work_item.id,
                "--evidence-database-url",
                db_url,
                "--evidence-mode",
                "forced_langgraph_true",
                "--json",
            ]
        )


def test_compare_script_merges_work_item_db_evidence_into_review_packet(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    merged_path = tmp_path / "merged-packet.json"
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_forced_true_db",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        sources=[
            WorkItemSourceRef(
                title="Mindful Care source",
                url="https://example.com/mindful-care-source",
            )
        ],
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "forced_langgraph_true",
            "--evidence-from-work-item",
            work_item.id,
            "--evidence-database-url",
            db_url,
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    forced_true = merged["mode_evidence"][2]

    assert exit_code == 0
    assert forced_true["work_item_id"] == work_item.id
    assert forced_true["route"] == "outreach_composer"
    assert forced_true["status"] == "done"
    assert forced_true["source_urls"] == ["https://example.com/mindful-care-source"]
    assert forced_true["workflow_sdk_usage_event"]["agent"] == "outreach_composer"
    assert forced_true["langgraph_orchestration_event"]["schema"] == (
        "keystone.langgraph.orchestration.v1"
    )
    assert forced_true["visible_output"] == ""
    assert forced_true["side_effects_reviewed"] is False
    assert output["live_output_review_decision"]["decision"] == "unreviewed"


def test_compare_script_rejects_work_item_db_graph_event_for_forced_off_merge(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    merged_path = tmp_path / "merged-packet.json"
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_forced_off_with_graph_event",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        sources=[
            WorkItemSourceRef(
                title="Mindful Care source",
                url="https://example.com/mindful-care-source",
            )
        ],
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )

    with pytest.raises(ValueError, match="must not include langgraph_orchestration_event"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--merge-mode-evidence",
                "forced_langgraph_false_control",
                "--evidence-from-work-item",
                work_item.id,
                "--evidence-database-url",
                db_url,
                "--write-review-packet",
                str(merged_path),
                "--json",
            ]
        )


def test_compare_script_rejects_work_item_db_missing_graph_event_for_forced_true_merge(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    merged_path = tmp_path / "merged-packet.json"
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_forced_true_missing_graph_event",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
        sources=[
            WorkItemSourceRef(
                title="Mindful Care source",
                url="https://example.com/mindful-care-source",
            )
        ],
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )

    with pytest.raises(ValueError, match="must include langgraph_orchestration_event"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--merge-mode-evidence",
                "forced_langgraph_true",
                "--evidence-from-work-item",
                work_item.id,
                "--evidence-database-url",
                db_url,
                "--write-review-packet",
                str(merged_path),
                "--json",
            ]
        )


def test_compare_script_db_merge_preserves_existing_side_effect_review(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet["mode_evidence"][2].update(
        {
            "side_effects": NO_SIDE_EFFECTS,
            "side_effects_reviewed": True,
        }
    )
    packet_path = tmp_path / "review-packet.json"
    merged_path = tmp_path / "merged-packet.json"
    db_url = f"sqlite:///{tmp_path / 'kba.db'}"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    store = SQLiteStore(db_url)
    work_item = WorkItem(
        id="wi_partial_db_preserve",
        kind=WorkItemKind.GMAIL_THREAD,
        status=WorkItemStatus.DONE,
        title="Gmail triage",
        current_route=WorkItemRoute.OUTREACH_COMPOSER,
    )
    store.save_work_item(work_item)
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            actor="outreach_composer",
            metadata={
                "schema": "keystone.workflow_sdk_usage.v1",
                "agent": "outreach_composer",
                "usage": {"requests": 1},
                "cost": {"estimated_usd": 0.002},
                "request_cache": {"static_prefix_sha256": "outreach-static"},
            },
        ),
    )
    store.save_work_item_event(
        work_item.id,
        WorkItemEvent(
            event_type="langgraph_orchestration",
            actor="langgraph",
            metadata={
                "schema": "keystone.langgraph.orchestration.v1",
                "node_path": ["run_gmail_triage", "run_outreach_composer"],
            },
        ),
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "forced_langgraph_true",
            "--evidence-from-work-item",
            work_item.id,
            "--evidence-database-url",
            db_url,
            "--write-review-packet",
            str(merged_path),
            "--json",
        ]
    )

    merged = json.loads(merged_path.read_text(encoding="utf-8"))
    forced_true = merged["mode_evidence"][2]

    assert exit_code == 0
    assert forced_true["work_item_id"] == work_item.id
    assert forced_true["workflow_sdk_usage_event"]["agent"] == "outreach_composer"
    assert forced_true["side_effects"] == NO_SIDE_EFFECTS
    assert forced_true["side_effects_reviewed"] is True


def test_compare_script_rejects_wrong_mode_evidence_merge(tmp_path) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "forced-evidence.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "schema": "keystone.langgraph.live_slack_evidence.v1",
                "mode": "forced_langgraph_true",
                "slack_permalink": "https://example.slack.com/forced",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match merge mode"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--merge-mode-evidence",
                "open_default_backend_selected",
                "--evidence-json",
                str(evidence_path),
            ]
        )


def test_compare_script_rejects_graph_event_in_forced_off_evidence_merge(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "forced-off-evidence.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "schema": "keystone.langgraph.live_slack_evidence.v1",
                "mode": "forced_langgraph_false_control",
                "slack_permalink": "https://example.slack.com/forced-off",
                "langgraph_orchestration_event": {
                    "schema": "keystone.langgraph.orchestration.v1",
                    "node_path": ["gmail_triage", "business_research"],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must not include langgraph_orchestration_event"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--merge-mode-evidence",
                "forced_langgraph_false_control",
                "--evidence-json",
                str(evidence_path),
            ]
        )


def test_compare_script_rejects_invalid_graph_event_schema_in_evidence_merge(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    evidence_path = tmp_path / "forced-true-evidence.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "schema": "keystone.langgraph.live_slack_evidence.v1",
                "mode": "forced_langgraph_true",
                "slack_permalink": "https://example.slack.com/forced-true",
                "langgraph_orchestration_event": {
                    "schema": "example.invalid",
                    "node_path": ["gmail_triage", "business_research"],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--merge-mode-evidence",
                "forced_langgraph_true",
                "--evidence-json",
                str(evidence_path),
            ]
        )


def test_compare_script_accepts_legacy_graph_event_schema_in_evidence_merge(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    merged_path = tmp_path / "merged-packet.json"
    evidence_path = tmp_path / "forced-true-evidence.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(
            {
                "schema": "keystone.langgraph.live_slack_evidence.v1",
                "mode": "forced_langgraph_true",
                "slack_permalink": "https://example.slack.com/forced-true",
                "langgraph_orchestration_event": {
                    "runtime": "langgraph",
                    "node_path": ["gmail_triage", "business_research"],
                },
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--merge-mode-evidence",
            "forced_langgraph_true",
            "--evidence-json",
            str(evidence_path),
            "--write-review-packet",
            str(merged_path),
        ]
    )

    merged = json.loads(merged_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert merged["mode_evidence"][2]["langgraph_orchestration_event"] == {
        "runtime": "langgraph",
        "node_path": ["gmail_triage", "business_research"],
    }


def test_compare_script_can_fail_when_review_packet_does_not_prove_graph_better(
    tmp_path,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    assert main(["--review-packet", str(packet_path), "--require-graph-better"]) == 3


def test_compare_script_scores_review_packet_without_manual_json_edit(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    _fill_required_mode_evidence(packet)
    packet_path = tmp_path / "review-packet.json"
    scored_path = tmp_path / "scored-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--score-review",
            "usefulness=graph",
            "--score-review",
            "detail=graph",
            "--score-review",
            "relevance=graph",
            "--score-review",
            "evidence_quality=graph",
            "--score-review",
            "safety_and_permissions=graph",
            "--score-review",
            "efficiency=tie",
            "--review-observation",
            "usefulness.control=control was less actionable",
            "--review-observation",
            "usefulness.graph=graph produced a clearer decision",
            "--review-observation",
            "detail.control=control had fewer stage details",
            "--review-observation",
            "detail.graph=graph preserved useful handoff detail",
            "--review-observation",
            "relevance.control=control was less specific",
            "--review-observation",
            "relevance.graph=graph stayed on the requested thread",
            "--review-observation",
            "evidence_quality.control=control preserved fewer evidence gaps",
            "--review-observation",
            "evidence_quality.graph=graph preserved evidence gaps",
            "--review-observation",
            "safety_and_permissions.control=control was safe",
            "--review-observation",
            "safety_and_permissions.graph=graph was safe and clearer",
            "--review-observation",
            "efficiency.control=control used comparable calls",
            "--review-observation",
            "efficiency.graph=graph used comparable calls",
            "--write-review-packet",
            str(scored_path),
            "--require-graph-better",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    scored = json.loads(scored_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert output["live_output_review_decision"]["decision"] == "graph_better"
    assert {
        item["criterion"]: item["winner"] for item in scored["review_questions"]
    } == {
        "usefulness": "graph",
        "detail": "graph",
        "relevance": "graph",
        "evidence_quality": "graph",
        "safety_and_permissions": "graph",
        "efficiency": "tie",
    }
    usefulness = next(
        item for item in scored["review_questions"] if item["criterion"] == "usefulness"
    )
    assert usefulness["control_observation"] == "control was less actionable"
    assert usefulness["graph_observation"] == "graph produced a clearer decision"


def test_compare_script_rejects_invalid_review_score(tmp_path) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid review winner"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--score-review",
                "usefulness=maybe",
            ]
        )


def test_compare_script_rejects_invalid_review_observation(tmp_path) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid review observation field"):
        main(
            [
                "--review-packet",
                str(packet_path),
                "--review-observation",
                "usefulness.summary=not accepted",
            ]
        )


def test_live_smoke_plan_blocks_when_offline_comparison_not_ready() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="example",
        request_text="example",
        comparison={
            "ready_for_live_smoke": False,
            "regression_markers": ["route_changed"],
            "side_effect_safe_both": False,
        },
    )
    report = render_langgraph_live_smoke_plan(plan)

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert "offline_comparison_not_ready" in plan["blockers"]
    assert "side_effect_safety_not_proven_offline" in plan["blockers"]
    assert "Ready for live smoke: no" in report
    assert "graph-event evidence" in report


def test_live_smoke_plan_blocks_when_all_scenarios_gate_not_ready() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-thread-draft",
        request_text="example",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        all_scenarios_ready=False,
        all_scenarios_summary={
            "ready": False,
            "scenario_count": 7,
            "blockers": ["scenario_not_ready:chief-context-opportunity"],
        },
    )
    report = render_langgraph_live_smoke_plan(plan)

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert "all_scenarios_offline_gate_not_ready" in plan["blockers"]
    assert "all-scenarios offline readiness gate is missing or not ready" in (
        plan["stop_conditions"]
    )
    assert "All-scenarios offline gate: no (7 scenarios)" in report


def test_live_smoke_plan_blocks_when_all_scenarios_gate_missing() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-thread-draft",
        request_text="example",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
    )
    report = render_langgraph_live_smoke_plan(plan)

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert "all_scenarios_offline_gate_not_ready" in plan["blockers"]
    assert "All-scenarios offline gate: missing" in report


def test_live_smoke_plan_preserves_cost_and_side_effect_controls() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-outreach",
        request_text="example",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    assert plan["ready_for_live_smoke"] is True
    assert plan["max_live_sdk_calls"] == 5
    assert plan["approved_max_live_sdk_calls"] == 5
    assert plan["live_api_test_budget"] == 5
    assert plan["approved_live_api_test_budget"] == 5
    assert plan["internal_sdk_request_review_threshold_per_run"] == 4
    assert plan["internal_sdk_request_complex_graph_candidate_threshold"] == 8
    assert plan["internal_sdk_request_policy"] == {
        "review_threshold_per_run": 4,
        "complex_graph_candidate_threshold": 8,
        "separate_from_live_api_test_budget": True,
        "threshold_type": "per_run_internal_sdk_requests",
        "migration_requirement": (
            "Raising the threshold toward 8 requires trace evidence that added "
            "calls improve relevance, detail, planning quality, or execution quality."
        ),
    }
    assert plan["max_live_workflow_runs_before_review"] == 1
    assert [item["mode"] for item in plan["run_sequence"]] == [
        "open_default_backend_selected",
        "forced_langgraph_false_control",
        "forced_langgraph_true",
    ]
    assert [item["environment"] for item in plan["run_sequence"]] == [
        {"KEYSTONE_WORKITEM_LANGGRAPH": "unset"},
        {"KEYSTONE_WORKITEM_LANGGRAPH": "false"},
        {"KEYSTONE_WORKITEM_LANGGRAPH": "true"},
    ]
    assert plan["run_sequence"][1]["when"] == "only_after_open_run_passes_and_budget_remains"
    assert plan["live_controls"] == {
        "live_sdk": True,
        "live_search": False,
        "cost_profile": "slack_smoke_limited",
        "hosted_web_search_max_calls": 0,
        "allow_manager_loop_repair": False,
        "include_contact_enrichment": False,
        "external_writes_enabled": False,
        "send_enabled": False,
        "gmail_drafts_enabled": False,
        "post_outside_current_thread_enabled": False,
    }
    assert "workflow_sdk_usage is missing after a live SDK run" in plan["stop_conditions"]
    assert "forced graph run lacks langgraph_orchestration_event evidence" in (
        plan["stop_conditions"]
    )
    assert "per-run internal SDK requests exceed the bounded-smoke review threshold" in (
        plan["stop_conditions"]
    )
    assert "visible prompt wording includes harness labels that change backend routing" in (
        plan["stop_conditions"]
    )
    assert plan["prompt_guidance"] == [
        "Keep the visible Slack prompt as a natural operator ask.",
        "Do not include harness labels such as open/default, forced true, forced false, or LangGraph.",
        "Apply graph mode selection through backend configuration or test harness state, not user-facing wording.",
        "Keep live SDK, live-search, cost, and no-side-effect controls in live_controls or harness state, not the visible prompt.",
    ]
    assert plan["slack_prompt_template"].startswith("@KNI example")


def test_live_smoke_plan_from_packet_recommends_forced_off_after_open_checkpoint() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)

    plan = langgraph_live_smoke_plan_from_packet(packet)
    report = render_langgraph_live_smoke_plan(plan)

    assert plan["ready_for_live_smoke"] is True
    assert plan["recommended_next_run"] == "forced_langgraph_false_control"
    assert plan["max_live_workflow_runs_before_review"] == 1
    assert "Recommended next run: forced_langgraph_false_control" in report


def test_live_smoke_plan_from_packet_recommends_forced_on_after_control_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    for item in packet["mode_evidence"]:
        if item["mode"] != "forced_langgraph_false_control":
            continue
        item.update(
            {
                "slack_permalink": "https://example.slack.com/forced-off",
                "work_item_id": "wi_forced_off",
                "route": "outreach_composer",
                "status": "done",
                "operator_status": "completed",
                "slack_display_title": "Business Agents Draft Ready",
                "visible_output": "Forced-off visible Slack output",
                "source_urls": ["https://example.com/forced-off/source"],
                "actual_environment": {"KEYSTONE_WORKITEM_LANGGRAPH": "false"},
                "side_effects": NO_SIDE_EFFECTS,
                "side_effects_reviewed": True,
                "workflow_sdk_usage_event": {
                    "schema": "keystone.workflow_sdk_usage.v1",
                    "usage": {"requests": 1},
                    "cost": {"estimated_usd": 0.001},
                    "request_cache": {
                        "static_prefix_sha256": "forced-off-static",
                        "dynamic_prompt_sha256": "forced-off-dynamic",
                    },
                },
            }
        )

    plan = langgraph_live_smoke_plan_from_packet(packet)

    assert plan["ready_for_live_smoke"] is True
    assert plan["recommended_next_run"] == "forced_langgraph_true"


def test_live_smoke_plan_from_packet_recommends_review_after_all_evidence() -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    _fill_required_mode_evidence(packet)

    plan = langgraph_live_smoke_plan_from_packet(packet)

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "score_live_output_review"
    assert plan["max_live_workflow_runs_before_review"] == 0


def test_compare_script_review_packet_prints_packet_aware_live_smoke_plan(
    tmp_path,
    capsys,
) -> None:
    packet = run_comparison(scenario="gmail-research-thread-draft")["live_output_review_packet"]
    _fill_open_default_mode_evidence(packet)
    packet_path = tmp_path / "review-packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")

    exit_code = main(
        [
            "--review-packet",
            str(packet_path),
            "--include-live-smoke-plan",
        ]
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Bounded live-smoke plan" in captured.out
    assert "Recommended next run: forced_langgraph_false_control" in captured.out
    assert "Open/default live smoke checkpoint" in captured.out


def test_live_smoke_plan_blocks_when_live_sdk_call_budget_exceeds_cap() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-outreach",
        request_text="example",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        max_live_sdk_calls=6,
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert plan["max_live_sdk_calls"] == 6
    assert plan["approved_max_live_sdk_calls"] == 5
    assert "live_sdk_call_budget_exceeds_approved_cap" in plan["blockers"]


def test_live_smoke_plan_blocks_when_live_sdk_call_budget_is_nonpositive() -> None:
    plan = langgraph_live_smoke_plan(
        scenario="gmail-research-outreach",
        request_text="example",
        comparison={
            "ready_for_live_smoke": True,
            "regression_markers": [],
            "side_effect_safe_both": True,
        },
        max_live_sdk_calls=0,
        all_scenarios_ready=True,
        all_scenarios_summary={"ready": True, "scenario_count": 7, "blockers": []},
    )

    assert plan["ready_for_live_smoke"] is False
    assert plan["recommended_next_run"] == "do_not_run_live"
    assert "live_sdk_call_budget_missing_or_nonpositive" in plan["blockers"]


def _mark_review_packet_graph_better(packet: dict) -> None:
    for question in packet["review_questions"]:
        criterion = question["criterion"]
        if criterion in packet["minimum_better_than_control"]:
            question["winner"] = "graph"
        elif criterion == "efficiency":
            question["winner"] = "tie"
        else:
            question["winner"] = "graph"
        question["control_observation"] = f"{criterion} control observation"
        question["graph_observation"] = f"{criterion} graph observation"


def _fill_required_mode_evidence(packet: dict) -> None:
    packet["all_scenarios_ready"] = True
    packet["all_scenarios_summary"] = {
        "ready": True,
        "scenario_count": 7,
        "blockers": [],
    }
    for item in packet["mode_evidence"]:
        mode = item["mode"]
        if mode not in {"forced_langgraph_false_control", "forced_langgraph_true"}:
            continue
        item.update(
            {
                "slack_permalink": f"https://example.slack.com/{mode}",
                "work_item_id": f"wi_{mode}",
                "route": "outreach_composer",
                "status": "done",
                "operator_status": "completed",
                "slack_display_title": "Business Agents Run Completed",
                "visible_output": f"{mode} visible Slack output",
                "source_urls": [f"https://example.com/{mode}/source"],
                "actual_environment": {
                    "KEYSTONE_WORKITEM_LANGGRAPH": (
                        "true" if mode == "forced_langgraph_true" else "false"
                    )
                },
                "side_effects": NO_SIDE_EFFECTS,
                "side_effects_reviewed": True,
                "workflow_sdk_usage_event": {
                    "schema": "keystone.workflow_sdk_usage.v1",
                    "usage": {"requests": 1},
                    "cost": {"estimated_usd": 0.001},
                    "request_cache": {
                        "static_prefix_sha256": f"{mode}-static",
                        "dynamic_prompt_sha256": f"{mode}-dynamic",
                    },
                },
                "langgraph_orchestration_event": (
                    {
                        "schema": "keystone.langgraph.orchestration.v1",
                        "node_path": ["chief_of_staff", "business_research"],
                    }
                    if mode == "forced_langgraph_true"
                    else {}
                ),
            }
        )


def _fill_open_default_mode_evidence(packet: dict) -> None:
    packet["all_scenarios_ready"] = True
    packet["all_scenarios_summary"] = {
        "ready": True,
        "scenario_count": 7,
        "blockers": [],
    }
    for item in packet["mode_evidence"]:
        if item["mode"] != "open_default_backend_selected":
            continue
        item.update(
            {
                "slack_permalink": "https://example.slack.com/open-default",
                "work_item_id": "wi_open_default",
                "route": "outreach_composer",
                "status": "done",
                "operator_status": "completed",
                "slack_display_title": "Business Agents Run Completed",
                "visible_output": (
                    "Draft-only Slack-thread output. No external message was sent, "
                    "no Gmail draft was created, and no external write was performed."
                ),
                "source_urls": ["https://example.com/open-default/source"],
                "side_effects": NO_SIDE_EFFECTS,
                "side_effects_reviewed": True,
                "workflow_sdk_usage_event": {
                    "schema": "keystone.workflow_sdk_usage.v1",
                    "usage": {"requests": 1},
                    "cost": {"estimated_usd": 0.001},
                    "request_cache": {
                        "static_prefix_sha256": "open-default-static",
                        "dynamic_prompt_sha256": "open-default-dynamic",
                    },
                },
            }
        )
