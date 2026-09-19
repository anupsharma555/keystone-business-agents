"""The real SDK sees scoped catalog metadata; no network or tool invocation is needed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from agents import (
    AgentOutputSchema,
    Model,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    function_tool,
    handoff,
)
from agents.exceptions import ModelBehaviorError
from openai import AsyncOpenAI

from keystone_agents import sdk
from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.capabilities.catalog import (
    CATALOG_MARKER,
    instruction_profile_text,
    runtime_capability_catalog,
)
from keystone_agents.capabilities.profile import compile_request_capability_profile
from keystone_agents.run import _sdk_request_cache_metadata
from keystone_agents.schemas.opportunity import (
    OpportunityScoutResult,
    SuppliedOpportunityResult,
)
from keystone_agents.specialist_agent_tools import (
    _NESTED_PARENT_REPLAY_BLOCKS_ATTR,
    _nested_tool_free_agent,
)


class CapturedInput(RuntimeError):
    pass


class CaptureModel(Model):
    """Stop at the SDK model boundary, preserving real builder tools and output schemas."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self, system_instructions, input, model_settings, tools, output_schema,
        handoffs, tracing, **kwargs,
    ):
        self.calls.append({
            "instructions": system_instructions,
            "input": input,
            "tools": [tool_name_for_policy(tool) for tool in tools],
            "output_schema": output_schema,
            "handoffs": handoffs,
        })
        raise CapturedInput("Expected local input capture; no model generation.")

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


def capture(agent):
    model = CaptureModel()
    with pytest.raises(CapturedInput):
        Runner.run_sync(
            agent, "Synthetic original request remains visible.",
            run_config=RunConfig(model=model, tracing_disabled=True),
        )
    assert len(model.calls) == 1
    call = model.calls[0]
    assert "Synthetic original request remains visible." in str(call["input"])
    call["catalog"] = json.loads(call["instructions"].split(CATALOG_MARKER + "\n")[1])
    return call


def tool_names(catalog):
    return [entry["name"] for entry in catalog["attached_tools"]]


def child_routes(catalog):
    return [entry["route"] for entry in catalog["attached_specialist_tools"]]


def test_actual_chief_model_input_has_only_attached_specialists_and_current_contract():
    agent = build_chief_of_staff_agent(
        include_specialist_tools=True, tool_scope_mode="full",
    )
    call = capture(agent)
    catalog = call["catalog"]
    expected = [
        tool.specialist_route_name for tool in agent.tools
        if getattr(tool, "specialist_route_name", None)
    ]
    assert child_routes(catalog) == expected
    assert len(expected) == 9
    assert tool_names(catalog) == call["tools"]
    assert catalog["output_contract"] == "ChiefOfStaffResult"
    assert catalog["attached_handoffs"] == []
    gmail = next(x for x in catalog["attached_specialist_tools"] if x["route"] == "gmail_triage")
    assert gmail["description"] == AGENT_REGISTRY["gmail_triage"].handoff_description
    assert gmail["mode"] == "read_plan"
    assert gmail["invocation_tool"] in call["tools"]
    assert "registered_tools" not in json.dumps(catalog)


def test_actual_gmail_model_input_contains_own_metadata_without_peer_dump():
    agent = build_gmail_triage_agent(
        provider_selection_mode=True, provider_tools_live=False, tool_scope_mode="full",
    )
    call = capture(agent)
    catalog = call["catalog"]
    assert tool_names(catalog) == ["query_gmail_message_summaries", "read_gmail_context"]
    assert tool_names(catalog) == call["tools"]
    assert child_routes(catalog) == []
    assert catalog["output_contract"] == "EmailTriageResult"
    assert set(x["id"] for x in catalog["own_skills"]) <= set(AGENT_REGISTRY["gmail_triage"].skills)
    assert all(x["body_loaded"] for x in catalog["own_skills"])
    assert all(
        set(x) == {"id", "description", "reference", "body_loaded"}
        for x in catalog["own_skills"]
    )
    assert call["instructions"].count(CATALOG_MARKER) == 1
    assert call["output_schema"].name() == "EmailTriageResult"


def test_compact_supplied_scout_keeps_one_owner_metadata_entry_and_no_tools():
    agent = build_opportunity_scout_agent(
        attach_tools=False, instruction_profile="supplied_evidence",
    )
    call = capture(agent)
    catalog = call["catalog"]
    assert tool_names(catalog) == call["tools"] == []
    assert child_routes(catalog) == catalog["attached_handoffs"] == []
    assert len(catalog["own_skills"]) == 1
    skill = catalog["own_skills"][0]
    assert skill["id"] == "opportunity_scout_specialist_contracts"
    assert skill["body_loaded"] is False
    assert "<!-- opportunity_scout_specialist_contracts/SKILL.md -->" not in call["instructions"]
    assert len(json.dumps(catalog)) < 1000
    assert agent.output_type is SuppliedOpportunityResult
    assert (
        catalog["output_contract"]
        == call["output_schema"].name()
        == SuppliedOpportunityResult.__name__
    )

    default_call = capture(build_opportunity_scout_agent(attach_tools=False))
    assert (
        default_call["catalog"]["output_contract"]
        == default_call["output_schema"].name()
        == OpportunityScoutResult.__name__
    )


def test_post_build_filter_and_tool_free_repair_clone_refresh_view_without_mutating_parent():
    parent = build_gmail_triage_agent(
        provider_selection_mode=True, provider_tools_live=False, tool_scope_mode="full",
    )
    parent.tools = [tool for tool in parent.tools if tool.name == "read_gmail_context"]
    assert tool_names(capture(parent)["catalog"]) == ["read_gmail_context"]
    assert tool_names(capture(_nested_tool_free_agent(parent))["catalog"]) == []
    assert tool_names(capture(parent.clone(tools=[]))["catalog"]) == []
    assert [tool.name for tool in parent.tools] == ["read_gmail_context"]


def test_consumed_chief_child_is_not_advertised_again_and_replay_callable_survives():
    chief = build_chief_of_staff_agent(
        include_specialist_tools=True, tool_scope_mode="full",
    )
    assert callable(chief.instructions)
    original = chief.instructions
    before = instruction_profile_text(chief)
    gmail = next(
        t for t in chief.tools if getattr(t, "specialist_route_name", None) == "gmail_triage"
    )
    gmail.is_enabled = False
    getattr(chief, _NESTED_PARENT_REPLAY_BLOCKS_ATTR)["synthetic"] = (
        "Synthetic accepted child evidence remains visible during repair."
    )
    call = capture(chief)
    assert "gmail_triage" not in child_routes(call["catalog"])
    assert gmail.name not in call["tools"]
    assert len(child_routes(call["catalog"])) == 8
    assert chief.instructions is original
    assert "Synthetic accepted child evidence" in call["instructions"]
    assert "Synthetic accepted child evidence" not in instruction_profile_text(chief)
    assert instruction_profile_text(chief) != before
    assert "function " not in instruction_profile_text(chief)


def test_unknown_enablement_is_conditional_and_callback_runs_only_for_sdk():
    calls = []

    def enabled(context, agent):
        calls.append(agent.name)
        return False

    @function_tool(is_enabled=enabled)
    def synthetic_read() -> str:
        raise AssertionError("A visibility test must not execute tools.")

    agent = sdk.build_sdk_agent(
        "gmail_triage", "Preserve these instructions.", None,
        tools=[synthetic_read], enforce_tool_policy=False,
    )
    assert runtime_capability_catalog(agent)["attached_tools"] == [
        {"name": "synthetic_read", "availability": "conditional"},
    ]
    assert calls == []
    call = capture(agent)
    assert calls == ["gmail_triage"]
    assert call["tools"] == []
    assert call["catalog"]["attached_tools"][0]["availability"] == "conditional"
    assert "Invoke\nonly tools actually offered by the SDK" in call["instructions"]


def test_actual_native_handoffs_follow_enabled_attachment_not_registry_maximum():
    target = build_gmail_triage_agent(include_tools=False)
    disabled = handoff(build_opportunity_scout_agent(attach_tools=False), is_enabled=False)
    agent = sdk.build_sdk_agent(
        "chief_of_staff", "Preserve existing output constraints.", None,
        handoffs=[handoff(target), disabled], enforce_tool_policy=False,
    )
    call = capture(agent)
    assert [x["route"] for x in call["catalog"]["attached_handoffs"]] == ["gmail_triage"]
    assert len(call["handoffs"]) == 1
    assert child_routes(call["catalog"]) == []


def test_static_profile_covers_actual_catalog_and_repeated_builds_are_stable(monkeypatch):
    monkeypatch.setenv("KEYSTONE_SDK_PROMPT_CACHE_SCOPE", "synthetic-catalog-test")
    agents = [build_chief_of_staff_agent(include_specialist_tools=True) for _ in range(2)]
    projections = [instruction_profile_text(a) for a in agents]
    assert projections[0] == projections[1]
    assert CATALOG_MARKER in projections[0]
    assert capture(agents[0])["instructions"] == projections[0]
    assert all("function " not in text for text in projections)
    keys = [sdk.prompt_cache_key_audit_metadata(sdk.agent_with_stable_prompt_cache_key(
        a, provider="openai", model_name="gpt-5.4-mini",
    )) for a in agents]
    assert keys[0]["prompt_cache_key_present"]
    assert keys[0] == keys[1]
    metadata = _sdk_request_cache_metadata(
        agent=agents[0], prompt="Only dynamic request text.", session=None, max_turns=1,
    )
    assert metadata["instructions_sha256"] == hashlib.sha256(projections[0].encode()).hexdigest()
    assert metadata["dynamic_prompt_chars"] == len("Only dynamic request text.")
    assert metadata["instruction_profile_status"] == "static_base_and_current_catalog"
    profile = compile_request_capability_profile(
        entrypoint="cli", agent=agents[0], execution_shape="agent_loop",
        prompt_profile="synthetic", max_turns=1,
    )
    assert profile.prompt_chars == len(projections[0])
    assert profile.prompt_sha256 == metadata["instructions_sha256"]


def test_unknown_callable_is_preserved_and_not_hashed_as_a_function_repr():
    calls = []

    async def instructions(context, agent):
        calls.append(agent.name)
        return "Resolved synthetic callable instructions."

    agent = sdk.build_sdk_agent("gmail_triage", instructions, None, tools=[])
    assert instruction_profile_text(agent) is None
    metadata = _sdk_request_cache_metadata(
        agent=agent, prompt="Synthetic request", session=None, max_turns=1,
    )
    assert metadata["instructions_sha256"] == ""
    assert metadata["instruction_profile_status"] == "unresolved_dynamic_callable"
    assert calls == []
    call = capture(agent)
    assert calls == ["gmail_triage"]
    assert "Resolved synthetic callable instructions." in call["instructions"]
    assert call["catalog"]["own_skills"][0]["body_loaded"] is None
    with pytest.raises(ValueError, match="explicit static instruction base"):
        compile_request_capability_profile(
            entrypoint="cli", agent=agent, execution_shape="agent_loop",
            prompt_profile="synthetic", max_turns=1,
        )



def test_changed_registry_description_reaches_existing_agent_without_a_second_catalog(monkeypatch):
    chief = build_chief_of_staff_agent(include_specialist_tools=True, tool_scope_mode="full")
    before = instruction_profile_text(chief)
    attached_before = [tool.name for tool in chief.tools]
    monkeypatch.setitem(
        AGENT_REGISTRY, "gmail_triage",
        replace(
            AGENT_REGISTRY["gmail_triage"],
            handoff_description="Synthetic revised Gmail ownership description.",
        ),
    )
    call = capture(chief)
    gmail = next(
        item for item in call["catalog"]["attached_specialist_tools"]
        if item["route"] == "gmail_triage"
    )
    assert gmail["description"] == "Synthetic revised Gmail ownership description."
    assert instruction_profile_text(chief) != before
    assert [tool.name for tool in chief.tools] == attached_before


def test_unregistered_helper_remains_plain_sdk_agent_without_a_catalog():
    agent = sdk.build_sdk_agent("budgeted_parent_loop", "Synthetic helper instructions.", None)
    assert type(agent) is sdk.Agent
    assert runtime_capability_catalog(agent) is None
    assert instruction_profile_text(agent) == agent.instructions


@pytest.mark.parametrize("route", ["chief_of_staff", "gmail_triage"])
def test_real_responses_http_body_contains_scoped_catalog_without_network(route):
    calls = []

    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "synthetic-response", "object": "response", "created_at": 1,
            "status": "incomplete", "model": "gpt-5.4-mini", "output": [],
            "incomplete_details": {"reason": "max_output_tokens"},
        })

    client = AsyncOpenAI(
        api_key="synthetic-not-a-credential", base_url="https://synthetic.example.test/v1",
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    agent = (
        build_chief_of_staff_agent(include_specialist_tools=True, tool_scope_mode="full")
        if route == "chief_of_staff"
        else build_gmail_triage_agent(
            provider_selection_mode=True, provider_tools_live=False, tool_scope_mode="full",
        )
    )
    with pytest.raises(ModelBehaviorError):
        Runner.run_sync(
            agent, "Synthetic transport boundary request.",
            run_config=RunConfig(
                model=OpenAIResponsesModel("gpt-5.4-mini", client), tracing_disabled=True,
            ),
        )
    assert len(calls) == 1
    body = calls[0]
    catalog = json.loads(body["instructions"].split(CATALOG_MARKER + "\n")[1])
    assert catalog["owner"] == route
    assert tool_names(catalog) == [tool["name"] for tool in body["tools"]]
    assert "Synthetic transport boundary request." in json.dumps(body["input"])
    assert body["text"]["format"]["schema"] == AgentOutputSchema(agent.output_type).json_schema()
    assert bool(child_routes(catalog)) is (route == "chief_of_staff")
