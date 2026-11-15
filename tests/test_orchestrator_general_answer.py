"""Route-only Orchestrator preflight can own one bounded informational answer."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.receipts.normalization import normalize_tool_output_receipt
from keystone_agents.schemas.orchestrator import OrchestratorGeneralAnswer, OrchestratorResult
from keystone_agents.tools.search_provider import SearchResult


@pytest.mark.parametrize("query", [
    "Quick question: What does “preprint” mean? Answer briefly in plain language.",
    "Can you briefly explain preprints?",
    "Tell me what a preprint is.",
    "How is a preprint different from a peer-reviewed paper?",
    "Define RSS in plain language.",
    "What does an Airtable record mean?",
])
def test_model_selected_general_answer_overrides_noun_based_advice(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    advice = infer_manual_request_plan(query)
    preflight = _selected_preflight(monkeypatch, query)
    assert preflight.route_result.route == "orchestrator"
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert preflight.manual_request_plan.target_agent == "orchestrator"
    assert preflight.manual_request_plan.provider_operations == []
    assert not preflight.manual_request_plan.requires_live_search
    assert advice.source == "heuristic"


@pytest.mark.parametrize(("query", "route", "search"), [
    (
        "Which saved youth-depression preprint is most worth review?",
        "preprints_context_agent", False,
    ),
    ("What recent RSS announcements matter to Keystone?", "rss_context_agent", False),
    ("What is the preprint from my RSS feed?", "rss_context_agent", False),
    (
        "What is the latest 2026 policy on preprints at this journal?",
        "business_research_analyst", True,
    ),
    ("Draft a short message explaining what a preprint is.", "outreach_composer", False),
    ("Write an email explaining what a preprint is.", "outreach_composer", False),
    ("Research how preprints work.", "business_research_analyst", True),
])
def test_model_specialist_choice_does_not_enter_general_answer(
    monkeypatch: pytest.MonkeyPatch, query: str, route: str, search: bool,
) -> None:
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route=route, workflow=[], routing_mode="llm",
            requires_live_search=search,
            rationale="The requested evidence or deliverable belongs to this specialist.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(query, live_orchestrator=True)
    assert preflight.route_result.route == route
    assert preflight.manual_request_plan.target_agent == route
    assert preflight.manual_request_plan.source != "canonical:orchestrator_general_answer"


def _selected_preflight(monkeypatch: pytest.MonkeyPatch, request: str) -> Any:
    monkeypatch.setattr(
        orchestrator,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            rationale="Answer one self-contained concept question.",
        ),
    )
    return orchestrator.run_orchestrator_preflight(request, live_orchestrator=True)


def _searched_preflight(monkeypatch: pytest.MonkeyPatch, request: str) -> Any:
    monkeypatch.setattr(
        orchestrator,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            requires_live_search=True, rationale="Look up one current public fact.",
        ),
    )
    return orchestrator.run_orchestrator_preflight(request, live_orchestrator=True)


def test_explicit_specialist_mention_remains_route_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Can you briefly explain what RSS is?"
    advice = infer_manual_request_plan(query, requested_agent="rss_context_agent")
    candidates = orchestrator._bounded_orchestrator_route_candidates({
        "manual_request_plan": advice.model_dump(mode="json"),
    })
    assert candidates is None  # Unscoped named advice does not prune model choices.
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            rationale="A short definition needs no saved feed record.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        query, requested_agent="rss_context_agent", live_orchestrator=True,
    )
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert preflight.manual_request_plan.provider_operations == []


def test_named_scout_heuristic_does_not_block_model_selected_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "What does preprint mean?"
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            rationale="This asks for a general explanation, not an opportunity record.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        query, requested_agent="opportunity_scout", live_orchestrator=True,
    )
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert preflight.manual_request_plan.target_agent == "orchestrator"


def test_named_specialist_misfit_can_route_to_public_research(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Research how preprints work using public sources."
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="business_research_analyst", workflow=[], routing_mode="llm",
            requires_live_search=True,
            rationale="Public research is needed rather than saved preprint history.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        query, requested_agent="preprints_context_agent", live_orchestrator=True,
    )
    assert preflight.route_result.route == "business_research_analyst"
    assert preflight.manual_request_plan.target_agent == "business_research_analyst"
    assert preflight.manual_request_plan.source != "canonical:orchestrator_general_answer"


def test_named_saved_record_request_retains_model_selected_specialist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "Which saved youth-depression preprint is most worth review?"
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="preprints_context_agent", workflow=[], routing_mode="llm",
            rationale="The saved preprint history must be read.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        query, requested_agent="preprints_context_agent", live_orchestrator=True,
    )
    assert preflight.route_result.route == "preprints_context_agent"
    assert preflight.manual_request_plan.target_agent == "preprints_context_agent"
    assert preflight.manual_request_plan.source != "canonical:orchestrator_general_answer"


def test_answer_builder_loads_unique_skill_without_tools() -> None:
    route_agent = orchestrator.build_orchestrator_agent(
        route_only=True, include_tools=False, compact_instructions=True,
    )
    assert route_agent.tools == []
    assert "general_answer_ownership" in str(route_agent.instructions)
    assert "current_source_choice" in str(route_agent.instructions)
    assert "opportunity_output_ownership" in str(route_agent.instructions)
    agent = orchestrator.build_orchestrator_general_answer_agent()
    assert agent.name == "orchestrator"
    assert agent.tools == []
    assert agent.output_type is OrchestratorGeneralAnswer
    assert "general_answer_ownership" in str(agent.instructions)
    assert "opportunity_output_ownership" in str(agent.instructions)
    assert "Orchestrator General Answer" in str(agent.instructions)
    assert "saved provider records" in str(agent.instructions)


def test_answer_phase_blocks_model_selected_saved_record_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    query = "Which saved youth-depression preprint is most worth review?"
    preflight = _selected_preflight(monkeypatch, query)
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setattr(cli, "_run_ask_work_item", lambda *_args, **_kwargs: pytest.fail())
    monkeypatch.setattr(
        cli, "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=OrchestratorGeneralAnswer(
                answer="The saved study A is best.", needs_source=True,
                source_need_reason="Saved preprint records were not read.",
            ),
            usage={"requests": 1}, cost={}, request_cache={},
            raw_result=SimpleNamespace(new_items=[]),
        ),
    )
    exit_code = cli.main([
        "ask", "--live-sdk", "--json", "--max-openai-requests", "2",
        "--database-url", f"sqlite:///{tmp_path / 'source-needed.db'}",
        "--input", query,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "source_needed"
    assert payload["public_result"]["completion_confirmed"] is False
    assert "saved study A" not in payload["slack_display_text"].lower()


@pytest.mark.parametrize(("answer", "needs_source", "expected_status"), [
    ("A preprint is a research manuscript shared before formal peer review.", False, "completed"),
    ("A preprint is explained at https://unverified.example/term.", False, "blocked"),
    ("As of 2026, all preprints follow a single review policy.", True, "blocked"),
])
@pytest.mark.parametrize("graph_enabled", [False, True], ids=("direct-backend", "graph-enabled"))
def test_default_unnamed_cli_general_answer_is_model_owned_and_tool_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    answer: str,
    needs_source: bool,
    expected_status: str,
    graph_enabled: bool,
) -> None:
    request = "Quick question: What does “preprint” mean? Answer briefly in plain language."
    preflight = _selected_preflight(monkeypatch, request)
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert not cli._preflight_requires_work_item(preflight, request_text=request)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setenv("KEYSTONE_WORKITEM_LANGGRAPH", "true" if graph_enabled else "false")
    monkeypatch.setattr(
        cli, "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail("General answer must not create a WorkItem."),
    )
    observed: dict[str, Any] = {}

    def fake_sdk(*, agent: Any, **kwargs: Any) -> Any:
        observed["agent"] = agent
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            output=OrchestratorGeneralAnswer(answer=answer, needs_source=needs_source),
            usage={"requests": 1},
            cost={}, request_cache={}, raw_result=SimpleNamespace(new_items=[]),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    exit_code = cli.main([
        "ask", "--live-sdk", "--json", "--max-openai-requests", "2", "--database-url",
        f"sqlite:///{tmp_path / 'general-answer.db'}", "--input", request,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload.get("entrypoint") == "orchestrator_general_answer", payload
    assert payload["selected_agent"] == "orchestrator"
    assert payload["status"] == expected_status
    assert payload["public_result"]["status"] == expected_status
    assert payload["public_result"]["completion_confirmed"] is (expected_status == "completed")
    assert payload["slack_display_text"] == payload["public_result"]["text"]
    assert observed["agent"].tools == []
    assert observed["kwargs"]["max_turns"] == 1
    assert "general_answer_ownership" in str(observed["agent"].instructions)


@pytest.mark.parametrize(("answer", "expected_status"), [
    ("The reported release is 3.X. Source: https://public.example/releases", "completed"),
    ("The reported release is 3.X.", "blocked"),
    ("See https://public.example/other for the current release.", "blocked"),
])
def test_default_unnamed_cli_simple_search_requires_exact_returned_link(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    answer: str,
    expected_status: str,
) -> None:
    query = "What is the current stable Python release? Cite the official page."
    preflight = _searched_preflight(monkeypatch, query)
    assert preflight.manual_request_plan.source == "canonical:orchestrator_general_answer"
    assert preflight.manual_request_plan.requires_live_search
    monkeypatch.setattr(cli, "cli_default_live_research", lambda: True)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setattr(
        cli, "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail("Simple search must not create a WorkItem."),
    )
    result = SearchResult(title="Official release", link="https://public.example/releases")
    receipt = normalize_tool_output_receipt("search_web", [result])
    assert receipt is not None
    invocations = [
        {"tool_name": "search_web", "invocation_index": 1, "status": "started"},
        {"tool_name": "search_web", "invocation_index": 1, "status": "completed"},
    ]
    monkeypatch.setattr(cli, "tool_invocation_journal", lambda: invocations)
    monkeypatch.setattr(cli, "tool_receipt_journal", lambda: [receipt])
    raw_result = SimpleNamespace(new_items=[
        {"type": "function_call", "call_id": "search-1", "name": "search_web"},
        {"type": "function_call_output", "call_id": "search-1", "output": json.dumps([
            result.model_dump(mode="json")
        ])},
    ])
    observed: dict[str, Any] = {}

    def fake_sdk(*, agent: Any, **kwargs: Any) -> Any:
        observed["agent"] = agent
        observed["kwargs"] = kwargs
        return SimpleNamespace(
            output=OrchestratorGeneralAnswer(answer=answer, needs_source=False),
            usage={"requests": 2},
            cost={}, request_cache={}, raw_result=raw_result,
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    exit_code = cli.main([
        "ask", "--live-sdk", "--live-search", "--json", "--max-openai-requests", "4",
        "--database-url", f"sqlite:///{tmp_path / 'searched-answer.db'}", "--input", query,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload["entrypoint"] == "orchestrator_general_answer"
    assert payload["status"] == expected_status
    assert payload["public_result"]["completion_confirmed"] is (expected_status == "completed")
    assert payload["search_answer_evidence"]["passed"] is (expected_status == "completed")
    assert [tool.name for tool in observed["agent"].tools] == ["search_web"]
    assert observed["kwargs"]["tool_call_budget_contract"].max_total_calls == 1
    assert observed["kwargs"]["max_turns"] == 3
    assert "current_source_choice" in str(observed["agent"].instructions)


@pytest.mark.parametrize("operator_flag,provider_enabled", [(False, True), (True, False)])
def test_simple_search_without_effective_live_gate_blocks_before_answer_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_flag: bool,
    provider_enabled: bool,
) -> None:
    query = "What is the current stable Python release? Cite the official page."
    preflight = _searched_preflight(monkeypatch, query)
    monkeypatch.setattr(cli, "cli_default_live_research", lambda: provider_enabled)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setattr(
        cli, "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("Current answer must wait for explicit live search."),
    )
    assert cli.main([
        "ask", "--live-sdk", *(["--live-search"] if operator_flag else []),
        "--json", "--max-openai-requests", "4",
        "--database-url", f"sqlite:///{tmp_path / 'blocked-search.db'}", "--input", query,
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["block_kind"] == "current_source_needed"
    assert payload["public_result"]["completion_confirmed"] is False
