from __future__ import annotations

import json


def test_gmail_read_to_draft_graph_pair_plan_is_matched_and_cost_bounded(
    require_local_evidence,
) -> None:
    path = require_local_evidence(
        "artifacts/test-pack/next-live-gmail-read-to-draft-graph-pair-plan.json"
    )
    plan = json.loads(path.read_text())

    assert plan["schema"] == "keystone.gmail_read_to_draft_graph_pair_plan.v1"
    assert plan["status"] == "offline_ready_live_not_started"
    assert plan["approval"] == {
        "status": "approved_by_operator",
        "approved_api_call_ceiling": 8,
        "allocated_api_calls_for_this_pair": 4,
        "allocated_workflow_runs": 2,
    }
    assert plan["cost_gate"]["minimum_credit_balance_usd_exclusive"] == 0.25
    assert "less than or equal to $0.25" in plan["cost_gate"]["rule"]
    assert plan["model_controls"]["maximum_openai_requests_per_mode"] == 2
    assert plan["model_controls"]["maximum_openai_requests_for_pair"] == 4
    assert plan["model_controls"]["retries_allowed"] == 0
    assert plan["model_controls"]["live_search"] is False
    assert plan["model_controls"]["manual_planner_live"] is False

    assert [mode["label"] for mode in plan["modes"]] == ["graph_off", "graph_on"]
    assert [mode["environment"]["KEYSTONE_WORKITEM_LANGGRAPH"] for mode in plan["modes"]] == [
        "false",
        "true",
    ]
    assert plan["operator_ask"].count("KBA_TEST_DRAFT") == 0
    assert "configured exact test recipient" in plan["operator_ask"]
    assert "latest marked validation email" in plan["operator_ask"]
    assert "same draft" in plan["operator_ask"]
    assert "explicit scoped approval" not in plan["operator_ask"]
    assert "Do not send" not in plan["operator_ask"]
    assert plan["identity_contract"]["latest_email_semantics"].startswith(
        "select the newest matching message"
    )
    assert plan["identity_contract"]["latest_thread_semantics"].startswith(
        "select the newest matching conversation"
    )


def test_gmail_read_to_draft_graph_pair_plan_requires_identity_cleanup_and_slack_proof(
    require_local_evidence,
) -> None:
    path = require_local_evidence(
        "artifacts/test-pack/next-live-gmail-read-to-draft-graph-pair-plan.json"
    )
    plan = json.loads(path.read_text())
    provider_evidence = "\n".join(plan["required_provider_evidence"])
    stop_conditions = "\n".join(plan["stop_conditions"])
    graph_off = "\n".join(plan["required_slack_evidence"]["graph_off"])
    graph_on = "\n".join(plan["required_slack_evidence"]["graph_on"])

    assert plan["identity_contract"]["draft_marker"] == "KBA_TEST_DRAFT"
    assert plan["identity_contract"]["same_thread_required"] is True
    assert plan["identity_contract"]["same_draft_required_within_each_mode"] is True
    assert "same exact draft identity" in provider_evidence
    assert "independent absence verification" in provider_evidence
    assert "revision creates a second draft" in stop_conditions
    assert "Graph runtime: not invoked" in graph_off
    assert "Graph runtime: invoked" in graph_on
    assert "node path" in graph_on
    assert "Invocation alone is not an enhancement" in plan["graph_enhancement_rule"]
