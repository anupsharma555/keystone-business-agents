"""Model-owned context reuse survives route-to-execution adaptation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents import AgentOutputSchema

from keystone_agents.agents import orchestrator as module
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult, OrchestratorResult


def _state(status="completed", correlation="same_thread"):
    return {
        "prior_agent_runs": [
            {
                "id": "synthetic-prior",
                "route": "gmail_triage",
                "status": status,
                "thread_correlation": correlation,
                "summary": "The newsletter describes a device. It discusses a program. "
                "Source: https://example.test/message",
            }
        ]
    }


def _route(owner="gmail_triage", mode=True):
    return OrchestratorResult(
        route=owner,
        workflow=[owner],
        routing_mode="llm",
        context_only_response=mode,
        rationale="Use existing context for a concise answer without another provider read.",
    )


def _slack_followup_envelope(request: str) -> str:
    return "\n".join(
        [
            "business agents continue this prior Slack thread.",
            f"Current user request (authoritative): {request}",
            "Prior task owner (advisory): gmail_triage",
            "Provider affinity: gmail",
            "Previous request: Summarize the selected newsletter in two sentences.",
            "Previous result title: Business Agents Result Ready",
            (
                "Previous result: The newsletter describes a device. It discusses a "
                "program. Source: https://example.test/message"
            ),
            f"User follow-up: {request}",
            "Continue the same agent task.",
        ]
    )


@pytest.mark.parametrize("owner", ["gmail_triage", "chief_of_staff", "business_research_analyst"])
def test_context_only_mode_becomes_canonical_tool_free_plan(monkeypatch, owner):
    monkeypatch.setattr(module, "_route_ambiguous_with_llm", lambda *_a, **_k: _route(owner))
    request = "Make that one sentence, keeping the source link."
    preflight = module.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=_state(),
    )
    plan = preflight.manual_request_plan
    assert preflight.execution_allowed and preflight.composition_admission.composition_allowed
    assert plan.source == "canonical:orchestrator_context_only"
    assert plan.target_agent == owner
    assert not plan.provider_operations and plan.provider_system == "unspecified"
    assert not plan.workflow and not plan.requires_durable_state
    assert cli._manual_plan_is_bounded_provider_free_response(
        plan,
        route=owner,
        input_text=request,
        provider_free_composition_allowed=True,
    )
    assert not preflight.composition_admission.provider_action_allowed


@pytest.mark.parametrize("state", [None, _state("failed"), _state(correlation="different_thread")])
def test_context_only_decision_never_silently_becomes_a_provider_read(monkeypatch, state):
    monkeypatch.setattr(module, "_route_ambiguous_with_llm", lambda *_a, **_k: _route())
    result = module.run_orchestrator_preflight(
        "Make that shorter.",
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=state,
    )
    assert not result.execution_allowed
    assert result.block_kind == "context_only_response_unavailable"


def test_free_text_rationale_does_not_promote_a_heuristic_plan(monkeypatch):
    monkeypatch.setattr(module, "_route_ambiguous_with_llm", lambda *_a, **_k: _route(mode=False))
    result = module.run_orchestrator_preflight(
        "Make that one sentence, keeping the Gmail link.",
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=_state(),
    )
    assert result.manual_request_plan.source == "heuristic"
    assert not result.composition_admission.composition_allowed


def test_request_needing_new_email_evidence_keeps_gmail_tools_and_is_not_context_only(
    monkeypatch,
):
    request = "Find a newer message from Example Health and summarize it."
    monkeypatch.setattr(
        module,
        "_route_ambiguous_with_llm",
        lambda *_a, **_k: _route(mode=False).model_copy(
            update={"rationale": "Fresh mailbox evidence is required."}
        ),
    )
    result = module.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=_state(),
    )

    assert result.execution_allowed
    assert result.manual_request_plan.source != "canonical:orchestrator_context_only"
    assert not result.composition_admission.composition_allowed
    assert not cli._should_run_direct_supplied_response(
        request,
        requested_route="gmail_triage",
        manual_plan=result.manual_request_plan,
    )
    agent = build_gmail_triage_agent(
        provider_tools_live=False,
        provider_selection_mode=True,
        request_text=request,
        manual_request_plan=result.manual_request_plan,
        tool_scope_mode="request_scoped",
    )
    assert [tool.name for tool in agent.tools] == [
        "query_gmail_message_summaries",
        "read_gmail_context",
    ]


def test_context_only_mode_cannot_promote_mutations_or_multiple_owners():
    write = ManualRequestPlan(provider_operations=["update"])
    assert module._context_only_manual_plan(write, _route(), "Update the provider.") is None
    multiple = _route().model_copy(update={"workflow": ["gmail_triage", "outreach_composer"]})
    assert module._context_only_manual_plan(ManualRequestPlan(), multiple, "Rewrite.") is None


def test_new_flag_is_model_visible_and_legacy_results_keep_existing_behavior():
    schema = AgentOutputSchema(OrchestratorPlanningResult).json_schema()
    assert schema["properties"]["context_only_response"]["type"] == "boolean"
    assert not OrchestratorResult.model_validate({"route": "gmail_triage"}).context_only_response


def test_explicit_table_shape_survives_context_only_projection():
    prior = ManualRequestPlan(ask_shape={"output_form": "table"})
    plan = module._context_only_manual_plan(prior, _route(), "Put those facts in a table.")
    assert plan.ask_shape.output_form == "table"


def test_title_without_prior_answer_is_not_reusable_context(monkeypatch):
    monkeypatch.setattr(module, "_route_ambiguous_with_llm", lambda *_a, **_k: _route())
    state = _state()
    state["prior_agent_runs"][0].update(summary="", title="Completed Gmail run")
    result = module.run_orchestrator_preflight(
        "Make that shorter.",
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=state,
    )
    assert not result.execution_allowed


@pytest.mark.parametrize(
    "followup_text",
    [
        "Make that one sentence, keeping the source link.",
        "Please shorten that while keeping the link.",
        "Retain the source link and make the wording tighter.",
        "Using the same source, explain in one sentence what the program changes.",
    ],
    ids=("one-sentence", "shorter", "retain-link", "explain-point"),
)
def test_context_only_executor_receives_prior_answer_and_renders_link_without_tools(
    monkeypatch,
    capsys,
    tmp_path,
    followup_text,
):
    from keystone_agents.schemas.execution_request import DirectAgentResponse

    envelope = _slack_followup_envelope(followup_text)
    execution_request = cli.build_execution_request(envelope)
    state = cli._slack_continuation_workflow_state(
        envelope,
        prior_agent=execution_request.continuation.prior_agent,
    )
    planning_text = cli.execution_request_planning_text(execution_request)
    assert execution_request.current_request == followup_text
    assert planning_text.endswith(f"Authoritative follow-up: {followup_text}")
    monkeypatch.setattr(module, "_route_ambiguous_with_llm", lambda *_a, **_k: _route())
    preflight = module.run_orchestrator_preflight(
        followup_text,
        requested_agent="chief_of_staff",
        live_orchestrator=True,
        workflow_state=state,
    )
    estimate = cli._estimate_ask_openai_requests(
        SimpleNamespace(
            context_file="",
            agent="chief_of_staff",
            max_manager_steps=3,
            live_search=False,
        ),
        input_text=followup_text,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="gmail_triage",
        manual_plan=preflight.manual_request_plan,
        effective_live_search=False,
        provider_free_composition_allowed=True,
        observed_orchestrator_requests=1,
    )
    assert estimate["min"] == estimate["mandatory_request_minimum"] == 2
    assert estimate["max"] == 2
    assert estimate["stages"] == [
        "orchestrator_preflight",
        "gmail_triage_direct_supplied_response_sdk",
    ]
    answer = (
        "The newsletter connects the device with the program, which is the requested "
        "concise explanation: https://example.test/message"
    )
    calls = []

    def fake_sdk(**kwargs):
        calls.append(kwargs)
        assert kwargs["agent"].tools == [] and kwargs["agent"].handoffs == []
        assert kwargs["max_turns"] == 1
        assert kwargs["typed_input"].original_request == followup_text
        assert "The newsletter describes a device." in kwargs["typed_input"].selected_context
        assert "https://example.test/message" in kwargs["typed_input"].selected_context
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    result = cli._run_direct_supplied_context_response_live(
        "gmail_triage",
        planning_text,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'result.db'}",
        execution_context=state,
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 0 and len(calls) == 1
    assert output["input"] == followup_text
    assert output["public_result"]["text"] == answer
    assert output["completion_confirmed"]
    assert output["tool_admission"]["tool_count"] == 0
    assert output["tool_execution"]["model_tool_call_count"] == 0
    assert output["tool_execution"]["workflow_called_tool_names"] == []
    assert output["tool_execution"]["workflow_called_helper_names"] == [
        "validate_output_constraints"
    ]
    assert output["tool_execution"]["provider_request_attempt_count"] == 0
    assert output["tool_execution"]["provider_receipt_count"] == 0
    assert output["deterministic_helper_execution"] == [
        {"name": "validate_output_constraints", "called": True, "passed": True},
        {"name": "assess_unsupported_outreach_claims", "called": False, "passed": True},
    ]
