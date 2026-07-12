from __future__ import annotations

import pytest

from keystone_agents.schemas.manager_validation import AdvancedManagerRouteDecision
from scripts.run_advanced_manager_live_correction import (
    CORRECTION_REQUEST,
    INITIAL_REQUEST,
    build_manager_validation_agent,
)


def test_live_manager_validation_agent_is_compact_and_no_tool() -> None:
    agent = build_manager_validation_agent()
    instructions = str(agent.instructions)

    assert agent.name == "orchestrator"
    assert agent.tools == []
    assert "<!-- advanced_manager_route_compact.md -->" in instructions
    assert "<!-- slack-posting-rules.md -->" not in instructions
    assert "<!-- memory_policy.md -->" in instructions
    assert "combines work owned by two or more specialists" in instructions
    assert "prior_direction_considered=true" in instructions
    assert len(instructions) < 25_000


def test_live_manager_scenario_is_explicitly_correction_based_and_no_send() -> None:
    assert "do not send" in INITIAL_REQUEST
    assert CORRECTION_REQUEST.startswith("Correction:")
    assert "do not scout opportunities" in CORRECTION_REQUEST
    assert "read-only company brief" in CORRECTION_REQUEST


def test_manager_decision_schema_rejects_side_effects_or_selected_rejection() -> None:
    with pytest.raises(ValueError, match="side effects"):
        AdvancedManagerRouteDecision(
            selected_owner="chief_of_staff",
            current_objective="Coordinate research.",
            operation_boundary="approval_checkpoint",
            latest_instruction_applied=True,
            side_effects_allowed=True,
            rationale="Cross-agent task.",
        )
    with pytest.raises(ValueError, match="also be rejected"):
        AdvancedManagerRouteDecision(
            selected_owner="business_research_analyst",
            rejected_owners=["business_research_analyst"],
            current_objective="Research only.",
            operation_boundary="read_only",
            latest_instruction_applied=True,
            rationale="Correction applied.",
        )
