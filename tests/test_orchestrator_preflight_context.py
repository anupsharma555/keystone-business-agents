from __future__ import annotations

import json
import sys

from keystone_agents.orchestrator.preflight_context import (
    MANUAL_REQUEST_PLAN_ENV,
    ORCHESTRATOR_PREFLIGHT_ENV,
    ORCHESTRATOR_ROUTE_RESULT_ENV,
    SPECIALIST_EXECUTION_CONTEXT_ENV,
    load_specialist_execution_context_from_env,
    orchestrator_preflight_context_text,
    orchestrator_preflight_env,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _install_preflight_env(
    monkeypatch,
    *,
    agent: str,
    intent: str,
    target: str = "",
    request_text: str = "",
) -> None:
    plan = ManualRequestPlan(
        source="parent_orchestrator",
        requested_agent=agent,
        target_agent=agent,
        intent=intent,
        primary_target=target,
        objective=f"Parent Orchestrator selected {agent}.",
    )
    preflight = {
        "request_text": request_text or f"Parent request for {agent}.",
        "advisory_only": True,
        "selected_agent": agent,
        "execution_allowed": True,
        "manual_request_plan": plan.model_dump(mode="json"),
        "route_result": {"route": agent, "refused": False},
    }
    monkeypatch.setenv(MANUAL_REQUEST_PLAN_ENV, plan.model_dump_json())
    monkeypatch.setenv(ORCHESTRATOR_PREFLIGHT_ENV, json.dumps(preflight))


def test_company_research_cli_preserves_parent_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_company_research as cli

    _install_preflight_env(
        monkeypatch,
        agent="business_research_analyst",
        intent="company_research",
        target="Curebase",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_company_research.py", "--request-text", "research Curebase", "--json"],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["source"] == "parent_orchestrator"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"


def test_company_research_review_uses_raw_orchestrator_request_not_target_label(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_company_research as cli

    request_text = "state of KNI 2026 summary; ignore stale tax payment context"
    captured: dict[str, object] = {}
    _install_preflight_env(
        monkeypatch,
        agent="business_research_analyst",
        intent="company_research",
        target="Curebase",
        request_text=request_text,
    )

    def fake_review(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "reviewed_by": "orchestrator",
            "review_mode": "deterministic",
            "status": "partial",
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "build_cli_orchestrator_review", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--request-text",
            request_text,
            "--orchestrator-review",
            "--json",
        ],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert captured["request_summary"] == request_text
    assert captured["request_summary"] != "Curebase"
    assert payload["orchestrator_review"]["status"] == "partial"


def test_opportunity_scout_cli_preserves_parent_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_opportunity_scout as cli

    _install_preflight_env(
        monkeypatch,
        agent="opportunity_scout",
        intent="opportunity_search",
        target="behavioral health AI",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_opportunity_scout.py", "--topic", "behavioral health AI", "--json"],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["source"] == "parent_orchestrator"
    assert payload["orchestrator_preflight"]["selected_agent"] == "opportunity_scout"
    assert payload["orchestrator_preflight"]["preflight_memo"]["raw_request"]


def test_opportunity_scout_live_search_planner_receives_orchestrator_memo(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_opportunity_scout as cli
    from keystone_agents.opportunity_scout.search_plan import infer_opportunity_search_plan
    from keystone_agents.schemas.opportunity import OpportunityScoutResult

    captured: dict[str, object] = {}
    request_text = "find 5 behavioral health AI partners and explain the search plan"
    _install_preflight_env(
        monkeypatch,
        agent="opportunity_scout",
        intent="opportunity_search",
        target="behavioral health AI partners",
        request_text=request_text,
    )

    def fake_resolve_opportunity_search_plan(request_text_arg, **kwargs):
        captured["request_text"] = request_text_arg
        captured["planner_context"] = kwargs.get("planner_context")
        return infer_opportunity_search_plan(
            request_text_arg, desired_count=kwargs["desired_count"]
        )

    def fake_run_opportunity_scout_live(**kwargs):
        captured["search_plan"] = kwargs.get("search_plan")
        return (
            OpportunityScoutResult(
                topic=str(kwargs.get("topic") or ""),
                dry_run=False,
                search_provider="fixture",
                search_queries=["behavioral health AI partners"],
                records=[],
            ),
            {"debug_notes": ["fake live retrieval"]},
        )

    monkeypatch.setattr(
        cli, "resolve_opportunity_search_plan", fake_resolve_opportunity_search_plan
    )
    monkeypatch.setattr(cli, "run_opportunity_scout_live", fake_run_opportunity_scout_live)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--topic",
            "behavioral health AI partners",
            "--live-search",
            "--live-search-plan",
            "--no-dry-run",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["orchestrator_preflight"]["preflight_memo"]["raw_request"] == request_text
    assert captured["search_plan"] is not None
    planner_context = str(captured["planner_context"])
    assert "Orchestrator preflight memo for this specialist run" in planner_context
    assert f'"raw_request": "{request_text}"' in planner_context
    assert '"selected_agent": "opportunity_scout"' in planner_context


def test_opportunity_scout_review_uses_raw_orchestrator_request_not_topic_label(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_opportunity_scout as cli

    request_text = "why did weekly opportunities post here, and should we rerun it?"
    captured: dict[str, object] = {}
    _install_preflight_env(
        monkeypatch,
        agent="opportunity_scout",
        intent="opportunity_search",
        target="behavioral health AI",
        request_text=request_text,
    )

    def fake_review(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "reviewed_by": "orchestrator",
            "review_mode": "deterministic",
            "status": "partial",
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "build_cli_orchestrator_review", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--topic",
            "behavioral health AI",
            "--orchestrator-review",
            "--json",
        ],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert captured["request_summary"] == request_text
    assert captured["request_summary"] != "behavioral health AI"
    assert payload["orchestrator_review"]["status"] == "partial"


def test_gmail_triage_cli_preserves_parent_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_gmail_triage as cli

    _install_preflight_env(monkeypatch, agent="gmail_triage", intent="gmail_triage")
    monkeypatch.setattr(sys, "argv", ["run_gmail_triage.py", "--json"])

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["source"] == "parent_orchestrator"
    assert payload["orchestrator_preflight"]["selected_agent"] == "gmail_triage"


def test_gmail_triage_review_uses_raw_orchestrator_request_not_fixture_label(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_gmail_triage as cli

    request_text = "review the selected email thread and flag only source-backed next steps"
    captured: dict[str, object] = {}
    _install_preflight_env(
        monkeypatch,
        agent="gmail_triage",
        intent="gmail_triage",
        target="selected email thread",
        request_text=request_text,
    )

    def fake_review(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "reviewed_by": "orchestrator",
            "review_mode": "deterministic",
            "status": "partial",
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "build_cli_orchestrator_review", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_gmail_triage.py", "--orchestrator-review", "--json"],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert captured["request_summary"] == request_text
    assert captured["request_summary"] != "gmail triage fixture run"
    assert payload["orchestrator_review"]["status"] == "partial"


def test_outreach_cli_preserves_parent_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_outreach_draft as cli

    _install_preflight_env(monkeypatch, agent="outreach_composer", intent="outreach_draft")
    monkeypatch.setattr(sys, "argv", ["run_outreach_draft.py", "--json"])

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["manual_request_plan"]["source"] == "parent_orchestrator"
    assert payload["orchestrator_preflight"]["selected_agent"] == "outreach_composer"


def test_outreach_review_uses_raw_orchestrator_request_not_fixture_label(
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_outreach_draft as cli

    request_text = (
        "draft outreach that references the original NeuroFlow research request "
        "and asks for source-backed context"
    )
    captured: dict[str, object] = {}
    _install_preflight_env(
        monkeypatch,
        agent="outreach_composer",
        intent="outreach_draft",
        target="NeuroFlow",
        request_text=request_text,
    )

    def fake_review(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "reviewed_by": "orchestrator",
            "review_mode": "deterministic",
            "status": "partial",
            "send_enabled": False,
        }

    monkeypatch.setattr(cli, "build_cli_orchestrator_review", fake_review)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--orchestrator-review", "--json"],
    )

    assert cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert captured["request_summary"] == request_text
    assert "sample_company_curebase" not in captured["request_summary"]
    assert payload["orchestrator_review"]["status"] == "partial"


def test_orchestrator_preflight_env_omits_raw_workflow_state() -> None:
    plan = ManualRequestPlan(
        source="parent_orchestrator",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="route_request",
        primary_target="architecture review",
    )
    env = orchestrator_preflight_env(
        {
            "request_text": "review this Slack thread",
            "selected_agent": "chief_of_staff",
            "advisory_only": True,
            "execution_allowed": True,
            "manual_request_plan": plan.model_dump(mode="json"),
            "route_result": {
                "route": "chief_of_staff",
                "routing_mode": "llm",
                "rationale": "Use Chief of Staff.",
                "refused": False,
                "workflow_state_summary": {
                    "slack_context": {"channel_id": "C123"},
                    "recent_slack_thread": [
                        {"summary": "private Slack thread text that should not cross env"}
                    ],
                    "prior_agent_runs": [
                        {"summary": "operator feedback that should not cross env"}
                    ],
                },
            },
        }
    )

    preflight = json.loads(env[ORCHESTRATOR_PREFLIGHT_ENV])
    route_result = json.loads(env[ORCHESTRATOR_ROUTE_RESULT_ENV])
    serialized = json.dumps(env, sort_keys=True)

    assert preflight["selected_agent"] == "chief_of_staff"
    assert preflight["route_result"]["route"] == "chief_of_staff"
    assert "workflow_state_summary" not in preflight["route_result"]
    assert "workflow_state_summary" not in route_result
    assert "private Slack thread text" not in serialized
    assert "operator feedback" not in serialized
    assert preflight["preflight_memo"]["orchestrator_route"] == "chief_of_staff"


def test_preflight_env_carries_only_explicit_bounded_specialist_context(monkeypatch) -> None:
    execution_context = {
        "schema": "keystone.direct_specialist_context.v1",
        "prior_agent_runs": [
            {
                "route": "zotero_context_agent",
                "object_id": "ITEM123",
                "title": "Example article",
            }
        ],
    }
    env = orchestrator_preflight_env(None, execution_context=execution_context)

    assert json.loads(env[SPECIALIST_EXECUTION_CONTEXT_ENV]) == execution_context
    assert ORCHESTRATOR_PREFLIGHT_ENV not in env

    monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, env[SPECIALIST_EXECUTION_CONTEXT_ENV])
    assert load_specialist_execution_context_from_env() == execution_context
    text = orchestrator_preflight_context_text(type("Args", (), {})())
    assert "ITEM123" in text
    assert "current operator request is authoritative" in text


def test_preflight_memo_includes_temporal_depth_policy() -> None:
    env = orchestrator_preflight_env(
        {
            "request_text": "Find the latest 2026 behavioral health AI partnerships.",
            "advisory_only": True,
            "execution_allowed": True,
            "route_result": {"route": "opportunity_scout", "refused": False},
        }
    )

    preflight = json.loads(env[ORCHESTRATOR_PREFLIGHT_ENV])
    policy = preflight["preflight_memo"]["temporal_depth_policy"]

    assert policy["schema"] == "keystone.temporal_depth_policy.v1"
    assert policy["temporal_intent"] is True
    assert {"latest", "2026"}.issubset(set(policy["trigger_terms"]))
    assert policy["independent_validation"] == "required_when_available"
    assert "not enough evidence yet" in policy["completion_rule"]
