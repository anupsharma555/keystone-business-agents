from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents import cli
from keystone_agents.direct_response import build_direct_supplied_response_agent
from keystone_agents.manual_request import infer_manual_request_plan, merge_manual_request_plan
from keystone_agents.planning.composition_admission import (
    resolve_provider_free_composition_admission,
)
from keystone_agents.schemas.execution_request import (
    DirectAgentResponse,
    DirectAgentResponseInput,
)
from keystone_agents.schemas.manual_request_plan import (
    AskShapePolicy,
    ManualProviderActionStep,
    ManualProviderResultSetScope,
    ManualRequestPlan,
    ProviderContextRequirement,
)
from keystone_agents.storage.sqlite_store import SQLiteStore

DIRECT_ROUTES = (
    "chief_of_staff",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "gmail_triage",
    "rss_context_agent",
    "preprints_context_agent",
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
    plan = infer_manual_request_plan(PROMPT, requested_agent=route)
    agent = build_direct_supplied_response_agent(
        route,
        request_text=PROMPT,
        manual_request_plan=plan,
    )

    assert agent.tools == []
    assert agent.output_type is DirectAgentResponse
    assert "direct_supplied_response.md" in str(agent.instructions)
    assert "do not reinterpret" in str(agent.instructions).lower()


def test_chief_attachment_response_uses_minimal_zero_tool_contract() -> None:
    request = (
        "give me the three points in this image as short bullets. Don't search or "
        "change anything.\nOperator-supplied Slack attachment local path: "
        "/private/tmp/kni-business-agent-slack-files/123/example.png"
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff").model_copy(
        update={"source": "llm"}
    )

    agent = build_direct_supplied_response_agent(
        "chief_of_staff",
        request_text=request,
        manual_request_plan=plan,
    )

    assert agent.tools == []
    assert agent.output_type is DirectAgentResponse
    assert "chief_of_staff_supplied_synthesis_compact" in str(agent.instructions)
    assert cli._should_run_direct_supplied_response(
        request,
        requested_route="chief_of_staff",
        manual_plan=plan,
    )


def test_missing_local_attachment_blocks_before_model_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-diagram.png"
    request = (
        "Looking at this diagram, summarize the three main stages. "
        "Don't search or change anything.\n"
        f"Operator-supplied Slack attachment local path: {missing_path}"
    )
    plan = infer_manual_request_plan(request, requested_agent="chief_of_staff")
    monkeypatch.setattr(
        cli,
        "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("model must not run without readable attachment bytes"),
    )

    exit_code = cli._run_direct_supplied_context_response_live(
        "chief_of_staff",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'attachment.db'}",
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "local_attachment_bytes_unavailable"
    assert payload["openai_requests"] == 0
    assert payload["tool_admission"]["tool_count"] == 0
    assert "reattach the file" in payload["human_summary"].lower()


def test_missing_local_attachment_blocks_before_orchestrator_preflight(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing-slack-image.png"
    request = (
        "@KNI CoS, what are the three main stages in this image? "
        "Don't search or change anything. "
        f"Operator-supplied Slack attachment local path: {missing_path}"
    )
    monkeypatch.setattr(
        cli,
        "run_orchestrator_preflight",
        lambda *_args, **_kwargs: pytest.fail(
            "Orchestrator must not run without readable attachment bytes"
        ),
    )

    exit_code = cli.main(
        [
            "ask",
            "--live-sdk",
            "--live-manual-plan",
            "--max-openai-requests",
            "2",
            "--database-url",
            f"sqlite:///{tmp_path / 'missing-attachment.db'}",
            "--json",
            request,
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["block_kind"] == "local_attachment_bytes_unavailable"
    assert payload["openai_requests"] == 0


def test_explicit_attachment_dry_run_does_not_create_work_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    attachment_path = tmp_path / "diagram.png"
    attachment_path.write_bytes(b"\x89PNG\r\n\x1a\nroute-fixture")
    database_path = tmp_path / "attachment-route.db"
    request = (
        "@KNI CoS, looking at this diagram, what are the three main stages a request "
        "goes through? Keep it to three short bullets. Don't search or change anything. "
        f"Operator-supplied Slack attachment local path: {attachment_path}"
    )
    monkeypatch.setattr(
        cli,
        "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail(
            "bounded attachment synthesis must not create a WorkItem"
        ),
    )

    exit_code = cli.main(
        [
            "ask",
            "--no-live-sdk",
            "--no-live-manual-plan",
            "--database-url",
            f"sqlite:///{database_path}",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["route"] == "chief_of_staff"
    assert payload["manual_request_plan"]["ask_shape"]["prior_context_dependency"] == (
        "selected_context"
    )
    assert payload["manual_request_plan"]["ask_shape"]["source_type_preference"] == [
        "local_attachment"
    ]
    assert SQLiteStore(f"sqlite:///{database_path}").list_work_items() == []


def test_explicit_attachment_live_cli_dispatches_one_direct_specialist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attachment_path = tmp_path / "diagram.png"
    attachment_path.write_bytes(b"\x89PNG\r\n\x1a\nlive-dispatch-fixture")
    request = (
        "@KNI CoS, looking at this diagram, what are the three main stages a request "
        "goes through? Keep it to three short bullets. Don't search or change anything. "
        f"Operator-supplied Slack attachment local path: {attachment_path}"
    )
    captured: dict[str, object] = {}

    def fake_direct(route: str, input_text: str, **kwargs: object) -> int:
        captured.update(route=route, input_text=input_text, kwargs=kwargs)
        return 0

    def fake_preflight(request_text: str, **kwargs: object):
        requested_agent = str(kwargs.get("requested_agent") or "chief_of_staff")
        plan = infer_manual_request_plan(
            request_text,
            requested_agent=requested_agent,
        ).model_copy(update={"source": "llm"})
        return cli.OrchestratorPreflight(
            request_text=request_text,
            requested_agent=requested_agent,
            advisory_only=True,
            selected_agent=plan.target_agent,
            manual_request_plan=plan,
            route_result=cli.route_request(request_text, manual_plan=plan),
            sdk_usage_events=[{"usage": {"requests": 1}}],
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(cli, "_run_direct_supplied_context_response_live", fake_direct)
    monkeypatch.setattr(
        cli,
        "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail(
            "bounded attachment synthesis must not enter the WorkItem executor"
        ),
    )

    exit_code = cli.main(
        [
            "ask",
            "--live-sdk",
            "--no-live-manual-plan",
            "--max-openai-requests",
            "3",
            "--database-url",
            f"sqlite:///{tmp_path / 'attachment-live-route.db'}",
            "--json",
            request,
        ]
    )

    assert exit_code == 0
    assert captured["route"] == "chief_of_staff"
    assert captured["input_text"] == request.removeprefix("@KNI CoS, ").strip()
    plan = captured["kwargs"]["manual_plan"]
    assert isinstance(plan, ManualRequestPlan)
    assert plan.ask_shape.prior_context_dependency == "selected_context"
    assert plan.ask_shape.source_type_preference == ["local_attachment"]


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


def test_structured_research_artifact_bypasses_generic_direct_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    followup = (
        "Make the second bullet a vendor question. Use the same note only. "
        "Do not search or use providers."
    )
    plan = infer_manual_request_plan(
        followup,
        requested_agent="business_research_analyst",
    ).model_copy(
        update={
            "source": "llm",
            "requires_live_search": False,
            "provider_system": "unspecified",
            "provider_operations": [],
        }
    )
    execution_context = {
        "thread_root_request": "Oakline supplies no baseline or sample.",
        "recent_thread_messages": [followup],
    }
    captured: dict[str, object] = {}

    def fake_company_research(
        input_text: str,
        **kwargs: object,
    ) -> int:
        captured.update(input_text=input_text, kwargs=kwargs)
        return 19

    monkeypatch.setattr(
        cli,
        "_run_direct_supplied_context_response_live",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic DirectAgentResponse must not replace research schema")
        ),
    )
    monkeypatch.setattr(cli, "_run_ask_company_research_live", fake_company_research)

    assert (
        cli._run_ask_specialist_live(
            "business_research_analyst",
            followup,
            json_output=True,
            manual_plan=plan,
            execution_context=execution_context,
        )
        == 19
    )
    assert captured["input_text"] == followup
    assert captured["kwargs"]["execution_context"] == execution_context


def test_workspace_read_dispatches_to_context_agent_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Google Workspace Context Agent, find README.doc in KNIOps, read it, and "
        "summarize its purpose and operating model without changing anything."
    )
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="google_workspace_context_agent",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        provider_system="google_workspace",
        provider_operations=["read"],
        primary_target="README.doc in KNIOps",
        target_type="business_system_context",
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            source_type_preference=["google_document"],
            permission_state="read_only",
        ),
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "_run_direct_supplied_context_response_live",
        lambda *_args, **_kwargs: pytest.fail(
            "provider-backed Workspace reads must not use the tool-free response lane"
        ),
    )

    def fake_context_agent(
        selected_route: str,
        input_text: str,
        **kwargs: object,
    ) -> int:
        captured.update(route=selected_route, input_text=input_text, kwargs=kwargs)
        return 23

    monkeypatch.setattr(cli, "_run_ask_context_agent_live", fake_context_agent)

    assert (
        cli._run_ask_specialist_live(
            "google_workspace_context_agent",
            request,
            json_output=True,
            manual_plan=plan,
        )
        == 23
    )
    assert captured["route"] == "google_workspace_context_agent"
    assert captured["input_text"] == request
    assert captured["kwargs"]["manual_plan"] == plan


def test_outreach_selected_context_dispatches_provider_free_after_preflight_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = (
        "Turn the reply outline into a draft using only the supplied email facts. "
        "Show it here for review. Do not access Gmail, create a provider draft, "
        "send, or modify anything."
    )
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="external",
        ),
    )
    route_result = cli.route_request(
        request,
        manual_plan=plan,
        workflow_state={
            "prior_agent_runs": [
                {
                    "route": "gmail_triage",
                    "status": "completed",
                    "thread_correlation": "same_thread",
                    "summary": "Reply outline: acknowledge interest and offer a short call.",
                }
            ]
        },
    )
    composition_admission = resolve_provider_free_composition_admission(
        plan,
        workflow_state={
            "prior_agent_runs": [
                {
                    "route": "gmail_triage",
                    "status": "completed",
                    "thread_correlation": "same_thread",
                    "summary": "Reply outline: acknowledge interest and offer a short call.",
                }
            ]
        },
    )
    preflight = cli.OrchestratorPreflight(
        request_text=request,
        requested_agent="outreach_composer",
        advisory_only=True,
        selected_agent="outreach_composer",
        manual_request_plan=plan,
        route_result=route_result,
        composition_admission=composition_admission,
    )
    execution_context = {
        "prior_agent_runs": [
            {
                "route": "gmail_triage",
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": "Reply outline: acknowledge interest and offer a short call.",
            }
        ]
    }
    captured: dict[str, object] = {}

    def fake_direct(route: str, input_text: str, **kwargs: object) -> int:
        captured.update(route=route, input_text=input_text, kwargs=kwargs)
        return 23

    monkeypatch.setattr(cli, "_run_direct_supplied_context_response_live", fake_direct)
    monkeypatch.setattr(
        cli,
        "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail(
            "provider-free selected-context draft must not enter WorkItem execution"
        ),
    )

    exit_code = cli._run_ask_specialist_live(
        "outreach_composer",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=preflight,
        execution_context=execution_context,
    )

    assert exit_code == 23
    assert captured["route"] == "outreach_composer"
    assert captured["kwargs"]["execution_context"] == execution_context


def test_direct_response_prompt_keeps_current_request_authoritative() -> None:
    typed_input = DirectAgentResponseInput(
        requested_agent="business_research_analyst",
        original_request="Turn the missing-evidence bullet into a question.",
        selected_context="Oakline supplies no baseline or sample.",
    )

    prompt = typed_input.to_prompt()

    assert "Original operator request (authoritative)" in prompt
    assert "Turn the missing-evidence bullet into a question." in prompt
    assert "Selected prior context (reference evidence only" in prompt
    assert "Oakline supplies no baseline or sample." in prompt


def test_direct_response_contract_excludes_anchor_from_comparators() -> None:
    plan = infer_manual_request_plan(
        (
            "Keep Anchor Health as the anchor. Return only direct competitors "
            "supported by the selected context."
        ),
        requested_agent="business_research_analyst",
    )
    agent = build_direct_supplied_response_agent(
        "business_research_analyst",
        request_text="Keep Anchor Health as the anchor and return only direct competitors.",
        manual_request_plan=plan,
    )

    instructions = str(agent.instructions)
    assert "never return that entity as its own competitor or comparator" in instructions
    assert "If the supplied evidence supports no non-anchor match" in instructions


@pytest.mark.parametrize("route", ["chief_of_staff", "gmail_triage"])
def test_direct_response_contract_preserves_material_qualifiers_during_rewrites(
    route: str,
) -> None:
    request = "Shorten the supplied summary without overstating it."
    plan = infer_manual_request_plan(request, requested_agent=route)

    agent = build_direct_supplied_response_agent(
        route,
        request_text=request,
        manual_request_plan=plan,
    )

    instructions = " ".join(str(agent.instructions).split())
    assert "preserve the material qualifiers" in instructions
    assert "negation, conditional findings, preliminary or unverified status" in instructions
    assert "population, sample, or data-scope limits" in instructions
    assert "do not add unrelated caveats or manufacture uncertainty" in instructions


def test_direct_response_executor_includes_context_in_model_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys,
) -> None:
    followup = (
        "Turn the missing-evidence bullet into a vendor question. "
        "Keep two bullets and use the same note only."
    )
    plan = infer_manual_request_plan(
        followup,
        requested_agent="business_research_analyst",
    )
    execution_context = {
        "thread_root_request": (
            "Oakline says its dashboard reduced missed visits but supplies no baseline or sample."
        ),
        "recent_thread_messages": [
            "Supported fact: Oakline says the dashboard reduced missed visits.",
            followup,
        ],
    }
    answer = (
        "- Supported fact: Oakline says its dashboard reduced missed visits.\n"
        "- Vendor question: What baseline and sample support that claim?"
    )
    fake_agent = SimpleNamespace(
        name="business_research_analyst",
        model="test-model",
        tools=[],
    )
    captured: dict[str, object] = {}

    def fake_sdk_run(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={"tool_count": 0},
        )

    monkeypatch.setattr(
        cli,
        "build_direct_supplied_response_agent",
        lambda *_args, **_kwargs: fake_agent,
    )
    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk_run)

    exit_code = cli._run_direct_supplied_context_response_live(
        "business_research_analyst",
        followup,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'followup.db'}",
        execution_context=execution_context,
    )

    assert exit_code == 0
    typed_input = captured["typed_input"]
    assert isinstance(typed_input, DirectAgentResponseInput)
    assert typed_input.original_request == followup
    assert "Oakline says its dashboard reduced missed visits" in typed_input.selected_context
    assert "baseline or sample" in typed_input.selected_context
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["human_summary"] == answer


def test_direct_outreach_response_blocks_inferred_organization_attribute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys,
) -> None:
    request = (
        "Use only this approved synthetic setup: Cedar Grove supports community clinics "
        "with behavioral-health measurement. Give me an internal organization "
        "introduction for review. Do not research, send, post, save, or update anything."
    )
    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")
    fake_agent = SimpleNamespace(name="outreach_composer", model="test-model", tools=[])
    fake_result = SimpleNamespace(
        output=DirectAgentResponse(
            answer="Cedar Grove is a physician-led care organization supporting community clinics."
        ),
        usage={"requests": 1},
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
        "outreach_composer",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'outreach-grounding.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "outreach_grounding_failed"
    assert payload["completion_confirmed"] is False
    assert payload["grounding_validation"]["passed"] is False
    assert payload["grounding_validation"]["unsupported_claims"] == ["physician-led"]
    assert "physician-led" in payload["human_summary"]
    assert payload["deterministic_helper_execution"] == [
        {
            "name": "validate_output_constraints",
            "called": True,
            "passed": True,
        },
        {
            "name": "assess_unsupported_outreach_claims",
            "called": True,
            "passed": False,
        },
    ]


def test_direct_outreach_does_not_pad_thin_supplied_facts_during_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys,
) -> None:
    request = (
        "Here's an approved synthetic setup: Lakeshore Quality Network supports "
        "independent practices that track behavioral-health outcomes and wants clearer "
        "monthly reporting. Nobody and no email address has been approved. Write a "
        "94-104 word organization introduction for internal review and then add "
        "Contact gap as its own line. Stick to that setup. Don't research, send, post, "
        "save, seek approval, or update CRM."
    )
    plan = infer_manual_request_plan(request, requested_agent="outreach_composer")
    candidate = (
        " ".join(["grounded"] * 80)
        + "\n\nContact gap: No contact or email address has been approved."
    )
    fake_agent = SimpleNamespace(name="outreach_composer", model="test-model", tools=[])
    fake_result = SimpleNamespace(
        output=DirectAgentResponse(answer=candidate),
        usage={"requests": 1},
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
    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("length-expansion repair must not run"),
    )

    exit_code = cli._run_direct_supplied_context_response_live(
        "outreach_composer",
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=f"sqlite:///{tmp_path / 'outreach-thin-evidence.db'}",
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "instruction_following_constraint_failed"
    assert payload["instruction_following"]["repair_attempted"] is False
    assert payload["instruction_following"]["repair_skipped_reason"] == (
        "strict_evidence_length_expansion_disabled"
    )
    assert payload["deterministic_helper_execution"][0] == {
        "name": "validate_output_constraints",
        "called": True,
        "passed": False,
    }


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

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=PROMPT,
        live_sdk=True,
        live_manual_plan=True,
        requested_route=route,
    )

    assert estimate["min"] == 3
    assert estimate["max"] == 4
    assert estimate["stages"] == [
        "manual_request_planner",
        "orchestrator_preflight",
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


@pytest.mark.parametrize(
    "request_text",
    [
        "Use backend browser diagnostics to inspect https://example.com for console errors.",
        "Check the rendered page and failed requests for https://example.com.",
        "Run read-only browser diagnostics on https://example.com and summarize network issues.",
    ],
)
def test_canonical_browser_diagnostics_never_use_provider_free_response_lane(
    request_text: str,
) -> None:
    plan = infer_manual_request_plan(
        request_text,
        requested_agent="business_research_analyst",
    ).model_copy(update={"source": "llm"})

    assert plan.intent == "browser_diagnostics"
    assert plan.target_agent == "chief_of_staff"
    assert not cli._should_run_direct_supplied_response(
        request_text,
        requested_route="chief_of_staff",
        manual_plan=plan,
    )


@pytest.mark.parametrize(
    ("route", "updates"),
    [
        (
            "gmail_triage",
            {
                "provider_action_steps": [
                    ManualProviderActionStep(
                        operation="read",
                        resource_type="gmail_thread",
                    )
                ],
            },
        ),
        (
            "gmail_triage",
            {
                "provider_context_requirements": [
                    ProviderContextRequirement(
                        provider_system="google_calendar",
                        resource_type="calendar_event",
                        purpose="match the related conversation",
                    )
                ],
            },
        ),
        (
            "zotero_context_agent",
            {
                "provider_result_scope": ManualProviderResultSetScope(
                    item_count=2,
                    item_refs=["candidate-a", "candidate-b"],
                ),
            },
        ),
        (
            "business_research_analyst",
            {
                "intent": "research_brief",
                "task_objective": "source_research",
                "expected_artifact_type": "research_brief",
            },
        ),
        (
            "opportunity_scout",
            {
                "intent": "opportunity_search",
                "task_objective": "opportunity_discovery",
                "expected_artifact_type": "opportunity_record",
                "provider_selection_order": "latest",
                "provider_selection_rank": 2,
                "desired_count": 2,
                "desired_count_explicit": True,
            },
        ),
        (
            "gmail_triage",
            {
                "intent": "gmail_triage",
                "task_objective": "gmail_triage",
                "expected_artifact_type": "gmail_triage_report",
            },
        ),
    ],
)
def test_specialist_or_provider_contracts_bypass_generic_direct_response(
    route: str,
    updates: dict[str, object],
) -> None:
    plan = infer_manual_request_plan(PROMPT, requested_agent=route).model_copy(
        update={"source": "llm", **updates}
    )

    assert not cli._should_run_direct_supplied_response(
        PROMPT,
        requested_route=route,
        manual_plan=plan,
    )


def test_opportunity_selection_contract_reaches_specialist_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = infer_manual_request_plan(PROMPT, requested_agent="opportunity_scout").model_copy(
        update={
            "source": "llm",
            "intent": "opportunity_search",
            "task_objective": "opportunity_discovery",
            "expected_artifact_type": "opportunity_record",
            "provider_selection_order": "latest",
            "desired_count": 2,
            "desired_count_explicit": True,
        }
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli,
        "_run_direct_supplied_context_response_live",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic response path must not intercept opportunity selection")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_run_ask_opportunity_scout_live",
        lambda input_text, **kwargs: captured.update(input_text=input_text, kwargs=kwargs) or 23,
    )

    assert (
        cli._run_ask_specialist_live(
            "opportunity_scout",
            PROMPT,
            json_output=True,
            manual_plan=plan,
        )
        == 23
    )
    assert captured["input_text"] == PROMPT


def test_internal_slack_copy_uses_bounded_provider_free_lane() -> None:
    request = (
        "Outreach Composer: Based only on this fact—Northstar Care has no audited "
        "outcomes—write one sentence for our internal Slack recommending the next "
        "step. Don't send email, create a provider draft, or search."
    )
    fallback = infer_manual_request_plan(request, requested_agent="outreach_composer")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Write one internal Slack recommendation from the supplied fact.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        outreach_channel="internal_slack",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
        ),
    )
    plan = merge_manual_request_plan(fallback, candidate)
    args = SimpleNamespace(
        agent=None,
        context_file="",
        live_search=False,
        max_manager_steps=3,
    )

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="outreach_composer",
        manual_plan=plan,
    )

    assert cli._should_run_direct_supplied_response(
        request,
        requested_route="outreach_composer",
        manual_plan=plan,
    )
    assert estimate["max"] == 4
    assert estimate["stages"] == [
        "manual_request_planner",
        "orchestrator_preflight",
        "outreach_composer_direct_supplied_response_sdk",
    ]


def test_internal_team_followup_uses_typed_provider_free_lane_despite_slack_drift() -> None:
    request = (
        "Combine those two points into a single paste-ready sentence for our "
        "internal team channel. Use only the Oakline note already in this thread; "
        "no search, email, or provider actions."
    )
    fallback = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Compose one internal team sentence from selected context.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        outreach_channel="team_channel",
        provider_system="slack",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="external",
        ),
    )
    plan = merge_manual_request_plan(fallback, candidate)

    assert cli._should_run_direct_supplied_response(
        request,
        requested_route="outreach_composer",
        manual_plan=plan,
    )


def test_outreach_executor_does_not_reparse_internal_typed_plan(
    monkeypatch,
) -> None:
    request = (
        "Combine those two points into one internal team sentence. "
        "Use only the selected thread context."
    )
    fallback = infer_manual_request_plan(request, requested_agent="outreach_composer")
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Compose one internal team sentence.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        outreach_channel="internal_slack",
        provider_system="unspecified",
        provider_operations=[],
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            prior_context_dependency="selected_context",
            permission_state="draft_only",
            audience_scope="internal",
        ),
    )
    plan = merge_manual_request_plan(fallback, candidate)
    captured: dict[str, object] = {}

    def fake_direct(route: str, input_text: str, **kwargs: object) -> int:
        captured.update({"route": route, "input_text": input_text, **kwargs})
        return 0

    monkeypatch.setattr(
        cli,
        "infer_outreach_execution_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy phrase heuristic must not run")
        ),
    )
    monkeypatch.setattr(cli, "_run_direct_supplied_context_response_live", fake_direct)

    exit_code = cli._run_ask_outreach_composer_live(
        request,
        json_output=True,
        manual_plan=plan,
        orchestrator_preflight=None,
        sdk_session_spec=None,
    )

    assert exit_code == 0
    assert captured["route"] == "outreach_composer"
    assert captured["manual_plan"] == plan


def test_budget_estimate_keeps_named_owner_for_internal_supplied_response() -> None:
    request = (
        "Outreach Composer: Based only on this fact—Northstar Care has no audited "
        "outcomes—write one sentence for our internal Slack recommending the next "
        "step. Don't send email, create a provider draft, or search."
    )
    fallback = infer_manual_request_plan(request, requested_agent="outreach_composer")
    incomplete_candidate = ManualRequestPlan(
        source="llm",
        requested_agent="outreach_composer",
        target_agent="outreach_composer",
        intent="outreach_draft",
        objective="Write one internal recommendation from the supplied fact.",
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        provider_system="unspecified",
        provider_operations=[],
        requires_live_search=False,
        requires_approved_context=True,
        side_effect_policy="draft_or_read_only",
    )
    plan = merge_manual_request_plan(fallback, incomplete_candidate)
    args = SimpleNamespace(
        agent="outreach_composer",
        context_file="",
        live_search=False,
        max_manager_steps=3,
    )

    assert plan.target_agent == "outreach_composer"
    assert plan.requires_approved_context is False
    assert cli._route_with_manual_plan_advice("outreach_composer", plan) == "outreach_composer"

    estimate = cli._estimate_ask_openai_requests(
        args,
        input_text=request,
        live_sdk=True,
        live_manual_plan=True,
        requested_route="outreach_composer",
        manual_plan=plan,
    )

    assert estimate["max"] == 4
    assert estimate["stages"] == [
        "manual_request_planner",
        "orchestrator_preflight",
        "outreach_composer_direct_supplied_response_sdk",
    ]


def test_direct_supplied_response_reserves_orchestrator_and_specialist(
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
            "3",
            "--database-url",
            database_url,
            "--json",
            PROMPT,
        ]
    )

    assert exit_code == 0
    assert captured["live_manual_plan"] is False
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
    assert payload["entrypoint"] == "direct_supplied_context_response"
    assert payload["output_authority"] == "synthesis_only"
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["request_cache"]["tool_count"] == 0
    assert payload["request_cache"]["output_authority"] == "synthesis_only"
    assert payload["tool_execution"]["model_tool_call_count"] == 0
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["tool_execution"]["provider_receipt_count"] == 0
    assert payload["public_result"]["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["public_result"]["omit_title"] is True
    assert payload["public_result"]["text"] == answer
