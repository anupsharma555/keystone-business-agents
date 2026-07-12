from __future__ import annotations

from scripts.run_compact_opportunity_baseline import build_baseline_agent


def test_baseline_agent_is_generic_no_tool_and_compact() -> None:
    agent = build_baseline_agent()
    instructions = str(agent.instructions)

    assert agent.name == "codex_chatgpt_baseline"
    assert agent.tools == []
    assert "Keystone profile" not in instructions
    assert "Shared Web Search Contract" not in instructions
    assert "perform no external action" in instructions
