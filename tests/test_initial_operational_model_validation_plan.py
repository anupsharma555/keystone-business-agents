from __future__ import annotations

import scripts.plan_initial_operational_model_validation as plan_module


def test_initial_model_plan_is_non_executing_and_budget_bounded() -> None:
    plan = plan_module.build_plan()
    assert plan["status"] == "plan_only"
    assert plan["execution_available"] is False
    assert plan["openai_requests_made"] == 0
    assert plan["model"] == "gpt-5.4-mini"
    assert plan["expected_requests"] == {"min": 7, "max": 10}
    assert plan["hard_stops"]["requests"] == 10
    assert plan["hard_stops"]["budget_usd"] == 2.0
    assert plan["live_search"] is False
    assert plan["provider_writes"] is False


def test_initial_model_plan_requires_fresh_browser_billing_observation() -> None:
    requirements = " ".join(plan_module.build_plan()["required_before_execution"])
    assert "refresh" in requirements.lower()
    assert "Chrome billing tab" in requirements
    assert "timestamp" in requirements


def test_initial_model_plan_covers_direct_continuation_and_graph() -> None:
    scenarios = {
        item["id"]: item for item in plan_module.build_plan()["scenarios"]
    }
    assert set(scenarios) == {
        "gmail_today_priority_summary",
        "gmail_selected_thread_draft",
        "gmail_same_session_revision",
        "graph_research_to_draft",
    }
    assert scenarios["gmail_same_session_revision"]["expected_requests_max"] == 1
    assert scenarios["graph_research_to_draft"]["expected_requests_max"] == 7
    assert all(not item["provider_writes"] for item in scenarios.values())
