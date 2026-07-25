from __future__ import annotations

from keystone_agents.agents.orchestrator import run_orchestrator_preflight


def test_manual_scout_ask_does_not_inherit_scheduled_automation_filters() -> None:
    current_request = (
        "Find behavioral-health AI partners that could fit KNI consulting. "
        "Separate exact matches from adjacent leads, say when evidence is thin, "
        "and don't save, post, or draft outreach."
    )
    prior_automation_request = (
        "Scheduled weekly scan: find Python agent workflow repositories with at "
        "least 50 stars and post the top five to Slack."
    )
    preflight = run_orchestrator_preflight(
        current_request,
        requested_agent="opportunity_scout",
        live_manual_plan=False,
        workflow_state={
            "execution_continuation": {
                "prior_agent": "opportunity_scout",
                "prior_request": prior_automation_request,
            },
            "recent_slack_thread": [
                {
                    "role": "agent",
                    "source_agent": "kni",
                    "summary": (
                        "Weekly automation found GitHub repositories filtered to "
                        "Python and stars >= 50; scheduled Slack post ready."
                    ),
                },
                {
                    "role": "operator",
                    "source_agent": "UUSER",
                    "summary": current_request,
                },
            ],
            "prior_agent_run_summaries": [
                {
                    "route": "opportunity_scout",
                    "status": "completed",
                    "summary": (
                        "Scheduled GitHub automation; language Python; minimum "
                        "stars 50; top 5."
                    ),
                }
            ],
        },
    )

    plan = preflight.manual_request_plan
    assert preflight.request_text == current_request
    assert preflight.selected_agent == "opportunity_scout"
    assert preflight.route_result.route == "opportunity_scout"
    assert plan.target_agent == "opportunity_scout"
    assert plan.intent == "opportunity_search"
    assert plan.objective == current_request
    assert plan.workflow == []
    assert plan.provider_system == "unspecified"
    assert plan.provider_operations == []
    assert plan.required_terms == []
    assert plan.required_entities == []
    assert plan.ask_shape.permission_state == "read_only"
    assert preflight.route_result.send_enabled is False

    executable_plan = " ".join(
        [
            plan.objective,
            plan.primary_target,
            *plan.constraints,
            *plan.required_terms,
            *plan.required_entities,
        ]
    ).lower()
    for stale_filter in ("github", "python", "50 stars", "top five", "weekly scan"):
        assert stale_filter not in executable_plan
