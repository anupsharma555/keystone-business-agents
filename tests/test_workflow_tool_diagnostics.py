"""A saved failure verdict must retain required evidence without returned bodies."""

import json

from keystone_agents.runtime.tool_diagnostics import compact_tool_diagnostics
from keystone_agents.workflow_runner import _record_workflow_sdk_cost_event


def _cache():
    return {
        "tool_execution_postcondition": {
            "schema": "keystone.tool_execution_postcondition.v1",
            "stage": "business_research_current_evidence",
            "mode": "required",
            "satisfied": False,
            "missing_groups": ["current_public_research"],
            "completed_tool_names": ["read_web_source_window"],
            "raw_source": "synthetic-private-body",
        },
        "source_window_visibility": [
            {
                "schema": "keystone.source_window_visibility.v1",
                "tool_names": ["read_web_source_window"],
                "evaluation_count": 20,
                "truncated": True,
                "observations": [
                    {
                        "limit": 8,
                        "remaining": 1,
                        "consumed": 7,
                        "enabled": False,
                        "text": "synthetic-private-body",
                    }
                ]
                * 20,
            }
        ],
        "raw_result": "synthetic-private-body",
    }


def test_failed_workflow_saves_tool_verdict_and_visibility(monkeypatch):
    from keystone_agents import workflow_runner

    saved = {}
    monkeypatch.setattr(
        workflow_runner, "record_event", lambda _work_item, **kwargs: saved.update(kwargs)
    )
    _record_workflow_sdk_cost_event(
        object(),
        event_type="workflow_sdk_usage",
        summary="Failed source read contract",
        agent_name="business_research_analyst",
        usage={},
        cost={},
        request_cache=_cache(),
        store=object(),
        run_stage="business_research.work_item_live_sdk_failed",
    )
    retained = saved["metadata"]["request_cache"]
    verdict = retained["tool_execution_postcondition"]
    assert verdict["satisfied"] is False
    assert verdict["missing_groups"] == ["current_public_research"]
    assert verdict["completed_tool_names"] == ["read_web_source_window"]
    visibility = retained["source_window_visibility"][0]
    assert visibility["evaluation_count"] == 20
    assert len(visibility["observations"]) == 12
    assert "not_provider_schema" in visibility["observation_kind"]
    assert "synthetic-private-body" not in json.dumps(saved["metadata"])


def test_missing_unknown_and_malformed_diagnostics_are_not_invented():
    assert compact_tool_diagnostics({}) == {}
    assert compact_tool_diagnostics({"tool_execution_postcondition": {"schema": "unknown"}}) == {}
    cache = _cache()
    cache["tool_execution_postcondition"]["mode"] = {}
    assert "tool_execution_postcondition" not in compact_tool_diagnostics(cache)
    cache = _cache()
    cache["source_window_visibility"][0]["observations"] = [{}]
    retained = compact_tool_diagnostics(cache)
    assert retained["source_window_visibility"][0]["observations"] == [{}]
    assert "synthetic-private-body" not in json.dumps(retained)
