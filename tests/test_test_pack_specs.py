from typing import get_args

from keystone_agents.schemas.feedback import FeedbackObjectType
from keystone_agents.schemas.memory import MemoryType
from keystone_agents.test_pack_specs import (
    SHARED_EVALUATION_CRITERIA,
    get_test_pack_spec,
    list_test_pack_specs,
    specs_by_agent,
)


def test_replacement_test_pack_has_five_specs_per_agent() -> None:
    specs = list_test_pack_specs()
    assert len(specs) == 25

    expected_ids = {
        *(f"GT-{index}" for index in range(1, 6)),
        *(f"BR-{index}" for index in range(1, 6)),
        *(f"OS-{index}" for index in range(1, 6)),
        *(f"OC-{index}" for index in range(1, 6)),
        *(f"OR-{index}" for index in range(1, 6)),
    }
    assert {spec.spec_id for spec in specs} == expected_ids

    grouped = specs_by_agent()
    assert set(grouped) == {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
    }
    assert all(len(agent_specs) == 5 for agent_specs in grouped.values())


def test_natural_prompts_stay_separate_from_harness_specs() -> None:
    for spec in list_test_pack_specs():
        assert spec.natural_prompt
        assert "Primary evaluation target" not in spec.natural_prompt
        assert "Pass criteria" not in spec.natural_prompt
        assert spec.primary_evaluation_target
        assert spec.pass_criteria


def test_specs_use_valid_feedback_and_memory_types() -> None:
    feedback_types = set(get_args(FeedbackObjectType))
    memory_types = set(get_args(MemoryType))

    for spec in list_test_pack_specs():
        assert spec.feedback_object_type in feedback_types
        assert set(spec.learning_memory_types) <= memory_types
        assert spec.learning_outputs_to_capture
        assert spec.learning_retention_notes


def test_orchestrator_uses_generic_feedback_object_type() -> None:
    spec = get_test_pack_spec("OR-1")

    assert spec.feedback_object_type == "other"
    assert "human_feedback" in spec.learning_memory_types
    assert "approval_decision" in spec.learning_memory_types


def test_report_spec_includes_learning_policy_without_polluting_prompt() -> None:
    spec = get_test_pack_spec("GT-2")
    report_spec = spec.to_report_spec()

    assert report_spec["object_type"] == "email_triage"
    assert report_spec["prompt"] == spec.natural_prompt
    assert "human_feedback" in report_spec["learning_memory_types"]
    assert any(
        "positive replies or feedback" in note for note in report_spec["learning_retention_notes"]
    )
    assert any(
        "positive-reply excerpts" in item for item in report_spec["learning_outputs_to_capture"]
    )


def test_shared_criteria_cover_safety_grounding_and_failure_behavior() -> None:
    criteria = {name for name, _description in SHARED_EVALUATION_CRITERIA}

    assert {"Grounding", "Boundary control", "Failure behavior"} <= criteria
