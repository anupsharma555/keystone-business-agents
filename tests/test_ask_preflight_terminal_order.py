"""A terminal planning result does not reserve an execution path that will not run."""

import json

import pytest

import keystone_agents.cli as cli
from keystone_agents.schemas.orchestrator import OrchestratorResult


@pytest.mark.parametrize("terminal", ["clarification", "refusal"])
def test_terminal_preflight_is_returned_before_post_planning_estimate(
    monkeypatch, capsys, tmp_path, terminal
):
    request = "Review the selected vendor newsletter and prepare a short internal template."
    message = (
        "Which of the two matching newsletters should I use?"
        if terminal == "clarification" else "This requested external action is not authorized."
    )
    plan = cli.infer_manual_request_plan(request, requested_agent="chief_of_staff")
    if terminal == "clarification":
        plan = plan.model_copy(update={"target_agent": "clarification"})
    route = "clarification" if terminal == "clarification" else "chief_of_staff"
    response = OrchestratorResult(
        route=route, target_agent=route, refused=terminal == "refusal",
        routing_mode="llm", rationale=message, stop_reason=message,
        clarification_request=message if terminal == "clarification" else None,
    )
    preflight = cli.OrchestratorPreflight(
        request_text=request, requested_agent="chief_of_staff", advisory_only=True,
        selected_agent=route, manual_request_plan=plan, route_result=response,
        blocked_by_orchestrator=terminal == "refusal",
        execution_allowed=terminal != "refusal",
        block_kind="approval" if terminal == "refusal" else "",
        block_reason=message if terminal == "refusal" else "",
        sdk_usage_events=[{"agent_name": "orchestrator", "usage": {"requests": 1}}],
    )
    preflight_calls = []

    def fake_preflight(*args, **kwargs):
        preflight_calls.append(True)
        return preflight

    def estimate(*args, **kwargs):
        assert kwargs.get("observed_orchestrator_requests") is None, (
            "A terminal preflight must not estimate an unexecuted downstream path"
        )
        return {
            "min": 3, "max": 100,
            "stage_rows": [{"stage": "chief", "model_controlled": True,
                            "conditional": False, "min_requests": 1}],
        }

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_estimate_ask_openai_requests", estimate)
    for function in ["_run_ask_work_item", "_run_ask_specialist_live"]:
        monkeypatch.setattr(cli, function, lambda *a, **kw: pytest.fail("No execution allowed"))
    cli.main([
        "ask", "--agent", "chief_of_staff", "--live-sdk", "--max-openai-requests", "8",
        "--database-url", f"sqlite:///{tmp_path / 'state.db'}", "--json", request,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert preflight_calls == [True]
    assert payload["status"] == ("needs_input" if terminal == "clarification" else "blocked")
    assert payload["message"] == message
    if terminal == "refusal":
        assert payload["human_summary"] == message
        assert payload["public_result"]["text"] == message
    assert payload["block_kind"] == (
        "clarification_required" if terminal == "clarification" else "approval"
    )
    assert "estimated_requests" not in payload
    assert payload["send_enabled"] is False
    assert payload["orchestrator_preflight"]["sdk_usage_events"][0]["usage"]["requests"] == 1
