from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from keystone_agents import cli
from keystone_agents.direct_response import build_direct_supplied_response_agent
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.schemas.execution_request import DirectAgentResponse

DIRECT_ROUTES = (
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "gmail_triage",
)

PROMPT = (
    "I’m heading into a meeting. Using only these two facts, give me exactly two "
    "short bullets: the shared admission layer now requires semantic intent plus a "
    "provider-bound action before any provider handler runs; exact response-count "
    "constraints should suppress decorative Slack titles. First bullet: what is now "
    "standardized. Second bullet: what this test confirms if it succeeds. Do not "
    "search, use tools or providers, create or modify anything, draft messages, or "
    "include routing or workflow metadata."
)


@pytest.mark.parametrize("route", DIRECT_ROUTES)
def test_direct_supplied_response_agents_admit_zero_tools(route: str) -> None:
    agent = build_direct_supplied_response_agent(route, request_text=PROMPT)

    assert agent.tools == []
    assert agent.output_type is DirectAgentResponse
    assert "direct_supplied_response.md" in str(agent.instructions)
    assert "do not reinterpret" in str(agent.instructions).lower()


@pytest.mark.parametrize("route", DIRECT_ROUTES)
def test_specialist_live_dispatch_uses_shared_provider_free_lane(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    plan = infer_manual_request_plan(PROMPT, requested_agent=route)
    captured: dict[str, object] = {}

    def fake_direct(
        selected_route: str,
        input_text: str,
        **kwargs: object,
    ) -> int:
        captured.update(route=selected_route, input_text=input_text, kwargs=kwargs)
        return 17

    monkeypatch.setattr(cli, "_run_direct_supplied_context_response_live", fake_direct)

    assert (
        cli._run_ask_specialist_live(
            route,
            PROMPT,
            json_output=True,
            manual_plan=plan,
        )
        == 17
    )
    assert captured["route"] == route
    assert captured["input_text"] == PROMPT


@pytest.mark.parametrize("route", DIRECT_ROUTES)
def test_direct_supplied_response_uses_planner_then_one_specialist_request(
    route: str,
) -> None:
    args = SimpleNamespace(
        agent=None,
        context_file="",
        live_search=False,
        max_manager_steps=3,
    )

    assert not cli._skip_live_manual_plan_for_request(
        PROMPT,
        requested_route=route,
    )
    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=PROMPT,
        live_sdk=True,
        live_manual_plan=True,
        requested_route=route,
    )

    assert estimate["min"] == 2
    assert estimate["max"] == 2
    assert estimate["stages"] == [
        "manual_request_planner",
        f"{route}_direct_supplied_response_sdk",
    ]


def test_direct_supplied_response_operational_context_never_changes_named_owner() -> None:
    plan = infer_manual_request_plan(
        PROMPT,
        requested_agent="business_research_analyst",
    )

    assert plan.target_agent == "business_research_analyst"
    assert plan.intent == "route_request"
    assert (
        cli._route_with_manual_plan_advice("business_research_analyst", plan)
        == "business_research_analyst"
    )
    assert cli._is_direct_supplied_response_request(
        PROMPT,
        requested_route="business_research_analyst",
    )


def test_direct_supplied_response_reaches_executor_under_one_request_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'direct-budget.db'}"
    captured: dict[str, object] = {}

    def recording_preflight(request_text: str, **kwargs: object):
        captured["live_manual_plan"] = kwargs.get("live_manual_plan")
        plan = infer_manual_request_plan(
            request_text,
            requested_agent="business_research_analyst",
        ).model_copy(update={"source": "llm"})
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent="business_research_analyst",
            advisory_only=True,
            selected_agent="business_research_analyst",
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    def fake_direct(
        route: str,
        input_text: str,
        **kwargs: object,
    ) -> int:
        captured.update(route=route, input_text=input_text, kwargs=kwargs)
        return 0

    monkeypatch.setattr(cli, "run_orchestrator_preflight", recording_preflight)
    monkeypatch.setattr(cli, "_run_direct_supplied_context_response_live", fake_direct)

    exit_code = cli.main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--live-sdk",
            "--max-openai-requests",
            "2",
            "--database-url",
            database_url,
            "--json",
            PROMPT,
        ]
    )

    assert exit_code == 0
    assert captured["live_manual_plan"] is True
    assert captured["route"] == "business_research_analyst"
    assert captured["input_text"] == PROMPT


@pytest.mark.parametrize("route", DIRECT_ROUTES)
def test_direct_provider_free_lane_returns_exact_public_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys,
    route: str,
) -> None:
    plan = infer_manual_request_plan(PROMPT, requested_agent=route)
    answer = "- Semantic intent and a provider-bound action are required.\n- The title is omitted."
    fake_agent = SimpleNamespace(name=route, model="test-model", tools=[])
    fake_result = SimpleNamespace(
        output=DirectAgentResponse(answer=answer),
        usage={"requests": 1, "input_tokens": 100, "output_tokens": 20},
        cost={"estimated_usd": 0.001},
        budget_guard={"status": "passed"},
        request_cache={"tool_count": 0},
    )
    monkeypatch.setattr(
        cli,
        "build_direct_supplied_response_agent",
        lambda *_args, **_kwargs: fake_agent,
    )
    monkeypatch.setattr(cli, "run_typed_sdk_agent", lambda **_kwargs: fake_result)

    exit_code = cli._run_direct_supplied_context_response_live(
        route,
        PROMPT,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / f'{route}.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["selected_agent"] == route
    assert payload["human_summary"] == answer
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["request_cache"]["tool_count"] == 0
    assert payload["public_result"]["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["public_result"]["omit_title"] is True
    assert payload["public_result"]["text"] == answer
