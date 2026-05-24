from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from keystone_agents.agent_registry import (
    AGENT_REGISTRY,
    REGISTERED_AGENT_SPECS,
    SPECIALIST_AGENT_SPECS,
    agent_cards,
    specialist_handoff_specs,
)
from keystone_agents.agent_tool_policy import (
    AgentToolPolicyError,
    disallowed_tool_names,
    tool_policy_for_agent,
)
from keystone_agents.agents.orchestrator import INTENDED_HANDOFFS, build_orchestrator_agent
from keystone_agents.sdk import Agent, build_sdk_agent, prompt_metadata_for_files
from keystone_agents.tools.gmail_tool import get_gmail_message

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_ROOT = PROJECT_ROOT / "src" / "keystone_agents" / "prompts"


def _tool_names(agent: Agent) -> set[str]:
    return {getattr(tool, "name", "") for tool in agent.tools}


def test_registry_has_canonical_agents() -> None:
    assert set(AGENT_REGISTRY) == {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
        "chief_of_staff",
    }
    assert [spec.route_name for spec in REGISTERED_AGENT_SPECS] == [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
        "chief_of_staff",
    ]


def test_registered_agents_have_builders_schemas_prompts_and_validation() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        assert spec.builder_name.startswith("build_")
        assert issubclass(spec.resolve_output_schema(), BaseModel)
        assert spec.handoff_description
        assert spec.safety_notes
        assert spec.eval_datasets or spec.validation_paths

        for prompt_file in spec.prompt_files:
            assert (PROMPTS_ROOT / prompt_file).exists()
        metadata = prompt_metadata_for_files(spec.prompt_files)
        assert all(item["name"] for item in metadata)
        assert all(item["version"] for item in metadata)

        for eval_path in (*spec.eval_datasets, *spec.validation_paths):
            assert (PROJECT_ROOT / eval_path).exists(), eval_path


def test_registered_builders_match_declared_schema_and_tools() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        agent = spec.build_agent()
        assert isinstance(agent, Agent)
        assert agent.output_type is spec.resolve_output_schema()
        assert set(spec.tools) <= _tool_names(agent)
        assert agent.handoff_description
        assert agent.input_guardrails
        assert agent.output_guardrails


def test_registered_agent_tools_follow_controlled_tool_policy() -> None:
    for spec in REGISTERED_AGENT_SPECS:
        policy = tool_policy_for_agent(spec.route_name)
        assert policy is not None
        agent = spec.build_agent()
        assert disallowed_tool_names(spec.route_name, sorted(_tool_names(agent))) == []

    outreach_policy = tool_policy_for_agent("outreach_composer")
    assert outreach_policy is not None
    assert "search_web" in outreach_policy.allowed_tool_names
    assert "get_gmail_message" not in outreach_policy.allowed_tool_names


def test_runtime_tool_policy_rejects_disallowed_tools() -> None:
    with pytest.raises(AgentToolPolicyError, match="disallowed tool"):
        build_sdk_agent(
            name="outreach_composer",
            instructions="Keystone test agent.",
            output_type=None,
            tools=[get_gmail_message],
            policy_agent_name="outreach_composer",
        )


def test_runtime_tool_policy_rejects_missing_policy() -> None:
    with pytest.raises(AgentToolPolicyError, match="No AgentToolPolicy"):
        build_sdk_agent(
            name="unregistered_agent",
            instructions="Keystone test agent.",
            output_type=None,
            tools=[],
            policy_agent_name="unregistered_agent",
        )


def test_orchestrator_handoffs_derive_from_specialist_registry() -> None:
    assert INTENDED_HANDOFFS == specialist_handoff_specs()

    agent = build_orchestrator_agent()
    assert len(agent.handoffs) == len(SPECIALIST_AGENT_SPECS)
    assert [handoff.name for handoff in agent.handoffs] == [
        spec.route_name for spec in SPECIALIST_AGENT_SPECS
    ]


def test_agent_cards_are_json_safe_extension_metadata() -> None:
    cards = agent_cards()

    assert len(cards) == len(REGISTERED_AGENT_SPECS)
    assert cards[0]["route_name"] == "gmail_triage"
    for card in cards:
        assert isinstance(card["prompt_files"], list)
        assert isinstance(card["tools"], list)
        assert isinstance(card["safety_notes"], list)
        assert "skills.md" in card["prompt_files"]
        assert "skills" not in card
        assert "capabilities" not in card
        assert "builder" in card
        assert "output_schema" in card
        assert card["tool_policy"] is not None
        assert isinstance(card["tool_policy"]["allowed_tool_names"], list)
