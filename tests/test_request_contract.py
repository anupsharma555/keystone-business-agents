"""Tests for the internal semantic-plan execution projection."""

from __future__ import annotations

from copy import deepcopy

from keystone_agents.authority.semantic import ExecutionIntentAuthority
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _canonical_plan(**updates: object) -> ManualRequestPlan:
    values: dict[str, object] = {
        "source": "llm",
        "requested_agent": "chief_of_staff",
        "target_agent": "business_research_analyst",
        "workflow": ["business_research_analyst"],
        "intent": "company_research",
        "objective": "Compare a named company with relevant peers.",
        "task_objective": "entity_research",
        "expected_artifact_type": "research_brief",
        "primary_target": "Anchor Health",
        "target_type": "company",
        "required_entities": ["Anchor Health"],
        "required_terms": ["multimodal", "behavioral health"],
        "requires_target_discovery": True,
        "anchor_entity": "Anchor Health",
        "desired_count": 4,
        "desired_count_explicit": True,
        "desired_count_mode": "minimum",
        "desired_count_scope": "total",
        "provider_system": "unspecified",
        "provider_operations": ["search", "read"],
        "requires_live_search": True,
        "side_effect_policy": "read_only",
        "ask_shape": {
            "evidence_depth": "deep",
            "source_type_preference": ["official", "independent"],
            "strict_filter_mode": "strict",
            "output_form": "table",
            "permission_state": "read_only",
            "audience_scope": "internal",
            "stop_condition": "Stop after four evidence-bearing targets.",
            "output_constraints": {
                "include_source_urls": True,
                "required_sections": ["Answer", "Evidence", "Limitations"],
            },
        },
    }
    values.update(updates)
    return ManualRequestPlan.model_validate(values)


def test_authority_compiles_orthogonal_request_contract_without_schema_churn() -> None:
    plan = _canonical_plan()
    original_payload = deepcopy(plan.model_dump(mode="json"))
    original_schema = ManualRequestPlan.model_json_schema()

    contract = ExecutionIntentAuthority.from_value(plan).compile_request_contract(
        raw_request="Use the plan, even if this sentence mentions email and outreach."
    )

    assert contract is not None
    assert contract.requested_agent == "chief_of_staff"
    assert contract.owner_agent == "business_research_analyst"
    assert contract.target.mode == "anchor_discovery"
    assert contract.target.anchor_entity == "Anchor Health"
    assert contract.target.required_entities == ("Anchor Health",)
    assert contract.evidence.required_dimensions == (
        "multimodal",
        "behavioral health",
    )
    assert contract.evidence.source_type_preferences == (
        "official",
        "independent",
    )
    assert contract.output.artifact_type == "research_brief"
    assert contract.output.output_form == "table"
    assert contract.capabilities.provider_operations == ("search", "read")
    assert contract.capabilities.permission_state == "read_only"
    assert contract.completion.desired_count == 4
    assert contract.completion.desired_count_mode == "minimum"
    assert contract.completion.desired_count_scope == "total"
    assert contract.completion.stop_condition
    assert plan.model_dump(mode="json") == original_payload
    assert ManualRequestPlan.model_json_schema() == original_schema
    assert "request_contract" not in original_schema.get("properties", {})


def test_target_topology_is_derived_from_typed_fields_not_request_wording() -> None:
    fixed = _canonical_plan(
        primary_target="",
        required_entities=["Alpha Health", "Beta Health"],
        requires_target_discovery=False,
        anchor_entity="",
        desired_count=2,
        desired_count_mode="exact",
    )
    authority = ExecutionIntentAuthority.from_value(fixed)

    first = authority.compile_request_contract(
        raw_request="Find competitors and discover ten more companies."
    )
    second = authority.compile_request_contract(raw_request="Only summarize the selected records.")

    assert first is not None and second is not None
    assert first.target == second.target
    assert first.completion == second.completion
    assert first.target.mode == "fixed_set"
    assert first.target.fixed_targets == ("Alpha Health", "Beta Health")
    assert first.completion.desired_count == 2
    assert first.completion.desired_count_mode == "exact"


def test_open_discovery_and_implicit_count_remain_distinct() -> None:
    plan = _canonical_plan(
        primary_target="behavioral-health AI companies",
        required_entities=[],
        requires_target_discovery=True,
        anchor_entity="",
        desired_count=1,
        desired_count_explicit=False,
        desired_count_mode="maximum",
        desired_count_scope="additional",
    )

    contract = ExecutionIntentAuthority.from_value(plan).request_contract

    assert contract is not None
    assert contract.target.mode == "open_discovery"
    assert contract.target.anchor_entity == ""
    assert contract.completion.desired_count is None
    assert contract.completion.desired_count_mode == "unspecified"
    assert contract.completion.desired_count_scope == "unspecified"


def test_requested_agent_is_audit_context_not_execution_ownership() -> None:
    plan = _canonical_plan(
        requested_agent="chief_of_staff",
        target_agent="business_research_analyst",
        provider_operations=[],
        requires_live_search=False,
        ask_shape={"permission_state": "read_only"},
    )

    contract = ExecutionIntentAuthority.from_value(plan).request_contract

    assert contract is not None
    assert contract.requested_agent == "chief_of_staff"
    assert contract.owner_agent == "business_research_analyst"
    assert contract.capabilities.provider_operations == ()
    assert contract.capabilities.live_search_requested is False


def test_effective_result_count_respects_plan_authority() -> None:
    canonical_implicit = ExecutionIntentAuthority.from_value(
        _canonical_plan(desired_count_explicit=False)
    )
    canonical_explicit = ExecutionIntentAuthority.from_value(_canonical_plan())
    compatibility_explicit = ExecutionIntentAuthority.from_value(
        {
            "source": "heuristic",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "desired_count": 6,
        }
    )
    compatibility_default = ExecutionIntentAuthority.from_value(
        {
            "source": "heuristic",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
        }
    )
    invalid = ExecutionIntentAuthority.from_value({"source": "llm", "target_agent": "not_an_agent"})

    assert canonical_implicit.effective_result_count() == 0
    assert canonical_explicit.effective_result_count() == 4
    assert compatibility_explicit.effective_result_count() == 6
    assert compatibility_default.effective_result_count() == 0
    assert invalid.request_contract is None
    assert invalid.effective_result_count() == 0


def test_contract_owns_deep_copies_of_mutable_nested_models() -> None:
    plan = _canonical_plan()
    contract = ExecutionIntentAuthority.from_value(plan).request_contract

    assert contract is not None
    plan.ask_shape.output_constraints.required_sections.append("Internal debug")

    assert contract.output.output_constraints.required_sections == [
        "Answer",
        "Evidence",
        "Limitations",
    ]
