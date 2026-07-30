from __future__ import annotations

from keystone_agents.agents.manual_request_planner import (
    MANUAL_REQUEST_PLANNER_MAX_INSTRUCTION_CHARS,
    MANUAL_REQUEST_PLANNER_PROMPTS,
    build_manual_request_planner_agent,
)
from keystone_agents.sdk import compose_instructions


def test_manual_request_planner_uses_compact_tool_free_profile() -> None:
    agent = build_manual_request_planner_agent()
    instructions = str(agent.instructions or "")

    assert len(instructions) <= MANUAL_REQUEST_PLANNER_MAX_INSTRUCTION_CHARS
    assert len(instructions) < 40_000
    assert agent.tools == []
    assert "Manual Request Planner" in instructions
    assert "Orchestrator is the first LLM control plane" in instructions

    # These general-purpose shared surfaces are useful to specialists and
    # renderers, but are irrelevant repeated prefix for the tool-free planner.
    assert "Memory And Pre-Run Context Policy" not in instructions
    assert "Slack Posting Rules" not in instructions
    assert "Keystone Profile" not in instructions
    assert "Writing Style Policy" not in instructions


def test_manual_request_planner_remains_substantially_smaller_than_full_profile() -> None:
    compact = str(build_manual_request_planner_agent().instructions or "")
    full_profile = compose_instructions(*MANUAL_REQUEST_PLANNER_PROMPTS)

    assert len(compact) <= len(full_profile) * 0.6


def test_compact_planner_retains_route_and_safety_authority() -> None:
    instructions = str(build_manual_request_planner_agent().instructions or "")

    for required_contract in (
        "The plan is pre-execution guidance for Python control code",
        "Never treat the plan itself as authority",
        "If the operator asks to send or publish",
        "Treat semantically equivalent asks as the same plan",
        "When bounded prior thread/work-item context is present",
        "Provider selection belongs in the shared retrieval policy",
        "You convert a natural-language Keystone manual request into a `ManualRequestPlan`",
    ):
        assert required_contract in instructions
