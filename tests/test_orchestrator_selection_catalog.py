"""The actual routing boundary exposes canonical capabilities without attaching tools."""

import json
from types import SimpleNamespace

import pytest

from keystone_agents.agent_registry import AGENT_REGISTRY, AgentSpec, agent_selection_catalog
from keystone_agents.agents import orchestrator
from keystone_agents.capabilities.profile import (
    active_runtime_route_scope,
    compile_runtime_route_scope,
)
from keystone_agents.runtime.decision_validation import (
    AgentDecisionValidationError,
    validate_specialist_decision,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import (
    OrchestratorPlanningResult,
    OrchestratorRouteDecision,
)


def test_route_catalog_respects_candidates_and_does_not_instantiate_agents(monkeypatch):
    monkeypatch.setattr(AgentSpec, "build_agent", lambda *a, **kw: pytest.fail("No builder"))
    cards = agent_selection_catalog(["gmail_triage", "clarification", "gmail_triage"])
    assert [card["route"] for card in cards] == ["gmail_triage"]
    assert cards[0]["registered_tools"] == list(AGENT_REGISTRY["gmail_triage"].tools)
    assert "get_gmail_message" in cards[0]["registered_tools"]
    assert "live_flags_required" not in cards[0]


def test_actual_preflight_model_input_contains_gmail_and_workspace_capabilities(monkeypatch):
    class Captured(Exception):
        pass

    captured = {}

    def stop_before_model(**kwargs):
        captured.update(kwargs)
        raise Captured

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", stop_before_model)
    request = "Read the selected email and summarize it without changing the mailbox."
    with pytest.raises(Captured):
        orchestrator._route_ambiguous_with_llm(
            request,
            approved_context_present=False,
            workflow_state={},
            live=True,
        )
    payload = captured["typed_input"]
    cards = {card["route"]: card for card in payload["specialist_catalog"]}
    assert payload["request"] == request
    assert "read_gmail_context" in cards["gmail_triage"]["registered_tools"]
    assert "query_gmail_message_summaries" in cards["gmail_triage"]["registered_tools"]
    assert "google_doc_read" in cards["google_workspace_context_agent"]["registered_tools"]
    assert "get_gmail_message" not in cards["google_workspace_context_agent"]["registered_tools"]
    assert captured["agent"].tools == []
    output_properties = captured["output_type"].model_json_schema()["properties"]
    agent_properties = captured["agent"].output_type.model_json_schema()["properties"]
    assert "provider_context_decisions" not in output_properties
    assert "provider_context_decisions" not in agent_properties
    assert "output_scopes" in captured["output_type"].model_json_schema()["properties"]
    assert captured["agent"].handoffs == []
    assert all(card["route"] in payload["allowed_routes"] for card in cards.values())
    assert "not attached tools or live permission" in " ".join(payload["hard_rules"])
    assert len(json.dumps(payload["specialist_catalog"])) < 20_000
    assert "effective_runtime_scope" not in payload


def test_active_preprints_scope_is_visible_before_model_and_validator_rejects_zotero(
    monkeypatch,
):
    class Captured(Exception):
        pass

    profile = SimpleNamespace(
        enabled=True,
        is_public_preprint_scenario=True,
        allowed_agents=frozenset(
            {
                "orchestrator",
                "chief_of_staff",
                "preprints_context_agent",
                "opportunity_scout",
                "instruction_following_repair",
            }
        ),
        allowed_function_tools=lambda agent: frozenset(
            {"retrieve_preprint_announcement_history"}
            if agent == "preprints_context_agent"
            else set()
        ),
    )
    monkeypatch.setattr(
        "keystone_agents.canary_acceptance.load_profile",
        lambda: profile,
    )
    scope = active_runtime_route_scope()
    assert scope is not None
    captured = {}

    def stop_before_model(**kwargs):
        captured.update(kwargs)
        raise Captured

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", stop_before_model)
    with pytest.raises(Captured):
        orchestrator._route_ambiguous_with_llm(
            "Review the saved article most relevant to clinical AI strategy.",
            approved_context_present=False,
            workflow_state={},
            live=True,
            runtime_scope=scope,
        )

    payload = captured["typed_input"]
    runtime_payload = payload["effective_runtime_scope"]
    assert "zotero_context_agent" not in payload["allowed_routes"]
    assert "zotero_context_agent" not in {
        card["route"] for card in payload["specialist_catalog"]
    }
    assert runtime_payload["scope_source"] == "reviewed_acceptance_profile"
    preprints = next(
        owner
        for owner in runtime_payload["owner_scopes"]
        if owner["route"] == "preprints_context_agent"
    )
    assert preprints["tool_names"] == ["retrieve_preprint_announcement_history"]
    assert preprints["source_kinds"] == ["local_saved_preprint_history"]
    serialized_scope = json.dumps(runtime_payload).lower()
    for forbidden in ("snapshot", "sha256", "credential", "answer", "gold"):
        assert forbidden not in serialized_scope

    invalid = OrchestratorPlanningResult(
        route="zotero_context_agent",
        workflow=["zotero_context_agent"],
        decision=OrchestratorRouteDecision(
            decision_owner="orchestrator",
            decision_stage="orchestrator_route_selection",
            selected_candidate_id="zotero_context_agent",
            reasoning="Selected an owner outside the current runtime ceiling.",
        ),
    )
    outcome, evidence = validate_specialist_decision(
        invalid,
        captured["decision_contract"],
    )
    assert outcome.status == "repair_required"
    assert outcome.reason_code == "selected_identity_not_in_candidate_set"
    assert "zotero_context_agent" not in evidence.candidate_ids


def test_same_runtime_scope_contract_supports_another_owner_and_source(monkeypatch):
    class Captured(Exception):
        pass

    captured = {}
    scope = compile_runtime_route_scope(
        scope_source="synthetic_offline_profile",
        allowed_agents=("orchestrator", "rss_context_agent"),
        allowed_function_tools={
            "rss_context_agent": ("retrieve_rss_announcement_history",),
        },
        source_kinds_by_agent={
            "rss_context_agent": ("local_saved_rss_history",),
        },
    )

    def stop_before_model(**kwargs):
        captured.update(kwargs)
        raise Captured

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", stop_before_model)
    with pytest.raises(Captured):
        orchestrator._route_ambiguous_with_llm(
            "Compare the stored industry announcements and summarize the recurring themes.",
            approved_context_present=False,
            workflow_state={},
            live=True,
            runtime_scope=scope,
        )

    payload = captured["typed_input"]
    assert payload["allowed_routes"] == ["rss_context_agent", "clarification"]
    assert payload["effective_runtime_scope"]["owner_scopes"] == [
        {
            "route": "rss_context_agent",
            "tool_names": ["retrieve_rss_announcement_history"],
            "source_kinds": ["local_saved_rss_history"],
        }
    ]
    assert captured["decision_contract"].pre_model_candidate_ids == (
        "rss_context_agent",
        "clarification",
    )


def test_restricted_scope_keeps_unavailable_explicit_owner_and_empty_scope_honest(
    monkeypatch,
):
    class Captured(Exception):
        pass

    captured = []

    def stop_before_model(**kwargs):
        captured.append(kwargs)
        raise Captured

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", stop_before_model)
    preprints_scope = compile_runtime_route_scope(
        scope_source="synthetic_offline_profile",
        allowed_agents=("orchestrator", "preprints_context_agent"),
        allowed_function_tools={
            "preprints_context_agent": ("retrieve_preprint_announcement_history",),
        },
        source_kinds_by_agent={
            "preprints_context_agent": ("local_saved_preprint_history",),
        },
    )
    unavailable_plan = ManualRequestPlan(
        requested_agent="zotero_context_agent",
        target_agent="zotero_context_agent",
        workflow=["zotero_context_agent"],
    )
    with pytest.raises(Captured):
        orchestrator._route_ambiguous_with_llm(
            "Ask the Zotero context agent to inspect this saved record.",
            approved_context_present=False,
            workflow_state={},
            routing_advice={
                "manual_request_plan": unavailable_plan.model_dump(mode="json"),
            },
            live=True,
            runtime_scope=preprints_scope,
        )
    unavailable_payload = captured[-1]["typed_input"]
    assert unavailable_payload["allowed_routes"] == ["clarification"]
    assert unavailable_payload["effective_runtime_scope"][
        "unavailable_requested_owner"
    ] == "zotero_context_agent"
    assert unavailable_payload["effective_runtime_scope"]["owner_scopes"] == []

    empty_scope = compile_runtime_route_scope(
        scope_source="synthetic_empty_profile",
        allowed_agents=("orchestrator", "instruction_following_repair"),
    )
    with pytest.raises(Captured):
        orchestrator._route_ambiguous_with_llm(
            "Review the stored source that supports the current recommendation.",
            approved_context_present=False,
            workflow_state={},
            live=True,
            runtime_scope=empty_scope,
        )
    empty_payload = captured[-1]["typed_input"]
    assert empty_payload["allowed_routes"] == ["clarification"]
    assert empty_payload["specialist_catalog"] == []
    assert captured[-1]["decision_contract"].pre_model_candidate_ids == (
        "clarification",
    )


def test_preflight_automatic_scope_discovery_cannot_restore_unavailable_named_owner(
    monkeypatch,
):
    profile = SimpleNamespace(
        enabled=True,
        is_public_preprint_scenario=True,
        allowed_agents=frozenset(
            {
                "orchestrator",
                "chief_of_staff",
                "preprints_context_agent",
                "instruction_following_repair",
            }
        ),
        allowed_function_tools=lambda agent: frozenset(
            {"retrieve_preprint_announcement_history"}
            if agent == "preprints_context_agent"
            else set()
        ),
    )
    monkeypatch.setattr(
        "keystone_agents.canary_acceptance.load_profile",
        lambda: profile,
    )
    captured = {}

    def reject_invalid_owner(**kwargs):
        captured.update(kwargs)
        invalid = OrchestratorPlanningResult(
            route="zotero_context_agent",
            workflow=["zotero_context_agent"],
            decision=OrchestratorRouteDecision(
                decision_owner="orchestrator",
                decision_stage="orchestrator_route_selection",
                selected_candidate_id="zotero_context_agent",
                reasoning="Selected the explicitly named but unavailable owner.",
            ),
        )
        outcome, _ = validate_specialist_decision(
            invalid,
            kwargs["decision_contract"],
        )
        assert outcome.status == "repair_required"
        raise AgentDecisionValidationError(outcome)

    monkeypatch.setattr(orchestrator, "run_typed_sdk_agent", reject_invalid_owner)
    with pytest.raises(AgentDecisionValidationError):
        orchestrator.run_orchestrator_preflight(
            "Ask the Zotero context agent to inspect this saved record.",
            requested_agent="zotero_context_agent",
            live_orchestrator=True,
        )

    assert captured["typed_input"]["allowed_routes"] == ["clarification"]
    assert captured["typed_input"]["effective_runtime_scope"][
        "unavailable_requested_owner"
    ] == "zotero_context_agent"


def test_route_scope_advice_cannot_reassign_explicit_draft_requirements(monkeypatch):
    from keystone_agents.schemas.orchestrator import OrchestratorResult

    scopes = {"word_scope": "answer", "item_scope": "answer", "source_scope": "answer"}
    result = OrchestratorResult(
        route="gmail_triage",
        workflow=["gmail_triage", "outreach_composer"],
        routing_mode="llm",
        output_scopes=scopes,
    )
    assert "OutputScopeBindings" in OrchestratorResult.model_json_schema()["$defs"]
    monkeypatch.setattr(orchestrator, "_route_ambiguous_with_llm", lambda *a, **kw: result)
    request = (
        "CoS, read the selected newsletter. In three bullets summarize it. "
        "Then write a 120–150-word exploratory outreach template here in Slack. "
        "Include its source link alongside the template. Do not send or change Gmail."
    )
    preflight = orchestrator.run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_orchestrator=True,
    )
    plan = preflight.manual_request_plan
    constraints = plan.ask_shape.output_constraints
    assert constraints.minimum_items == constraints.maximum_items == 3
    assert (constraints.minimum_words, constraints.maximum_words) == (120, 150)
    assert constraints.word_scope == "draft_body" and constraints.item_scope == "answer"
    assert constraints.include_source_urls is True
    assert constraints.source_scope == "entire_response"
    assert plan.source != "llm"  # Output binding is not blanket canonical-plan authority.


def test_route_only_schema_omits_execution_decisions_even_if_specialists_enabled(monkeypatch):
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_SPECIALIST_TOOLS", "true")
    planning = orchestrator.build_orchestrator_agent(route_only=True)
    assert planning.tools == [] and planning.handoffs == []
    assert (
        "provider_context_decisions" not in planning.output_type.model_json_schema()["properties"]
    )
    regular = orchestrator.build_orchestrator_agent(include_handoffs=False)
    assert "provider_context_decisions" in regular.output_type.model_json_schema()["properties"]


def test_planning_excludes_host_owned_fields_but_retains_decisions_and_safety():
    from pydantic import ValidationError

    from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult, OrchestratorResult

    fields = OrchestratorPlanningResult.model_json_schema()["properties"]
    host_fields = {
        "provider_context_decisions",
        "artifacts",
        "intended_handoffs",
        "workflow_state_summary",
        "state_context_used",
        "decision_trace",
        "target_agent",
        "routing_mode",
        "audit_notes",
        "approved_context_present",
        "send_enabled",
        "can_send_email",
    }
    assert not host_fields.intersection(fields)
    assert {
        "route",
        "workflow",
        "decision",
        "rationale",
        "output_scopes",
        "clarification_request",
        "refused",
        "retrieval_hint",
    } <= fields.keys()
    assert host_fields <= OrchestratorResult.model_json_schema()["properties"].keys()
    # Omitting model-authored grants does not remove the backend safety invariant.
    with pytest.raises(ValidationError, match="cannot enable sending"):
        OrchestratorPlanningResult(send_enabled=True)
    with pytest.raises(
        ValidationError,
        match="Tool-free planning cannot assert provider candidate selections",
    ):
        OrchestratorPlanningResult(
            provider_context_decisions=[
                {
                    "decision_owner": "orchestrator",
                    "decision_stage": "provider_context_selection",
                    "needs_more_context": True,
                }
            ]
        )
    result = orchestrator._post_process_llm_route(
        OrchestratorPlanningResult(route="gmail_triage", workflow=["gmail_triage"]),
        request_text="Read a selected email.",
        approved_context_present=False,
        workflow_state={},
    )
    assert result.routing_mode == "llm" and result.target_agent
    assert result.intended_handoffs and result.provider_context_decisions == []
    assert result.send_enabled is False and result.can_send_email is False


@pytest.mark.parametrize(
    "workflow,refused",
    [
        (["gmail_triage", "outreach_composer"], False),
        (["business_research_analyst", "outreach_composer"], False),
        (["outreach_composer"], True),
        (["outreach_composer", "gmail_triage"], True),
    ],
)
def test_planning_does_not_require_evidence_before_a_planned_acquisition(workflow, refused):
    from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult

    result = orchestrator._post_process_llm_route(
        OrchestratorPlanningResult(route="outreach_composer", workflow=workflow),
        request_text="Read the selected email, then draft a reply here. Do not send.",
        approved_context_present=False,
        workflow_state={},
    )
    assert result.refused is refused
    assert result.approved_context_present is False
    assert result.send_enabled is False
    if not refused:
        assert result.workflow == workflow


@pytest.mark.parametrize("selected,steps,expected", [
    ("gmail_triage", ["outreach_composer"], ["gmail_triage", "outreach_composer"]),
    ("gmail_triage", ["gmail_triage", "outreach_composer"], ["gmail_triage", "outreach_composer"]),
    (
        "outreach_composer",
        ["gmail_triage", "outreach_composer"],
        ["gmail_triage", "outreach_composer"],
    ),
])
def test_selected_specialist_is_not_dropped_from_executable_workflow(selected, steps, expected):
    from keystone_agents.schemas.orchestrator import OrchestratorPlanningResult
    result = orchestrator._post_process_llm_route(
        OrchestratorPlanningResult(route=selected, workflow=steps),
        request_text="Read the selected email then draft a template here. Do not send.",
        approved_context_present=False, workflow_state={},
    )
    assert not result.refused
    assert result.route == selected
    assert result.workflow == expected
