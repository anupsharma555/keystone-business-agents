from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.cli import (
    _interpreted_lifecycle_scope_text,
    _is_bounded_composite_lifecycle_request,
)
from keystone_agents.manual_request import (
    infer_manual_request_plan,
    merge_manual_request_plan,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

FIXTURE = Path("evals/static/semantic_routing_variations.json")
PLANNER_PROMPT = Path("src/keystone_agents/prompts/manual_request_planner.md")
LIFECYCLE_ROUTES = {
    "airtable_context_agent",
    "google_workspace_context_agent",
    "gmail_triage",
}


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _interpreted_plan(prompt: str, expected: dict) -> ManualRequestPlan:
    mention = parse_agent_mention(prompt)
    fallback = infer_manual_request_plan(
        mention.input_text,
        requested_agent=mention.route if mention.explicit else None,
    )
    candidate = ManualRequestPlan(
        source="llm",
        requested_agent=fallback.requested_agent,
        target_agent=expected["target_agent"],
        intent=expected["intent"],
        primary_target=expected["primary_target"],
        target_type=(
            "business_system_context"
            if expected["intent"] == "business_system_write"
            else "topic"
        ),
        objective=expected["objective"],
        task_objective=(
            "business_system_write"
            if expected["intent"] == "business_system_write"
            else (
                "opportunity_discovery"
                if expected["intent"] == "opportunity_search"
                else "route_or_continue"
            )
        ),
        expected_artifact_type=(
            "business_system_write_plan"
            if expected["intent"] == "business_system_write"
            else (
                "opportunity_record"
                if expected["intent"] == "opportunity_search"
                else "none"
            )
        ),
        constraints=expected["constraints"],
        requires_live_search=expected["provider"] == "shared_retrieval",
        side_effect_policy=expected["side_effect_policy"],
    )
    return merge_manual_request_plan(
        fallback,
        candidate,
        allow_contextual_delegation=True,
    )


def test_planner_prompt_makes_semantic_equivalence_an_llm_responsibility() -> None:
    prompt = PLANNER_PROMPT.read_text(encoding="utf-8")

    assert "Treat semantically equivalent asks as the same plan" in prompt
    assert "Imperative verbs are not required" in prompt
    assert "Restate the actual stages explicitly in `objective`" in prompt
    assert "Do not add a lifecycle stage that the operator did not request" in prompt
    assert "test markers waive approval" in prompt


def test_semantic_routing_fixture_has_five_ordinary_forms_per_group() -> None:
    payload = _payload()

    assert payload["schema"] == "keystone.semantic_routing_variations.v1"
    assert len(payload["groups"]) >= 5
    for group in payload["groups"]:
        variants = group["variants"]
        assert len(variants) >= 5
        forms = {variant["form"] for variant in variants}
        assert len(forms) == len(variants)
        assert {
            "direct imperative",
            "passive voice",
        } <= forms or group["id"] in {
            "opportunity_discovery",
            "chief_operational_prioritization",
        }


def test_correct_llm_interpretation_is_not_vetoed_by_surface_phrasing() -> None:
    for group in _payload()["groups"]:
        expected = group["expected"]
        for variant in group["variants"]:
            plan = _interpreted_plan(variant["prompt"], expected)

            assert plan.target_agent == expected["target_agent"], variant
            assert plan.intent == expected["intent"], variant
            assert plan.primary_target == expected["primary_target"], variant
            assert plan.side_effect_policy == expected["side_effect_policy"], variant
            assert set(expected["constraints"]) <= set(plan.constraints), variant


def test_equivalent_provider_lifecycles_reach_one_direct_owner_gate() -> None:
    for group in _payload()["groups"]:
        expected = group["expected"]
        if expected["target_agent"] not in LIFECYCLE_ROUTES:
            continue
        for variant in group["variants"]:
            mention = parse_agent_mention(variant["prompt"])
            plan = _interpreted_plan(variant["prompt"], expected)
            interpreted_scope = _interpreted_lifecycle_scope_text(
                mention.input_text,
                plan,
            )

            assert expected["execution_shape"] == "direct_single_owner"
            assert _is_bounded_composite_lifecycle_request(
                expected["target_agent"],
                input_text=interpreted_scope,
            ), variant


def test_equivalent_read_only_groups_preserve_owner_and_no_write_boundary() -> None:
    for group in _payload()["groups"]:
        expected = group["expected"]
        if expected["intent"] == "business_system_write":
            continue
        plans = [
            _interpreted_plan(variant["prompt"], expected)
            for variant in group["variants"]
        ]

        assert {plan.target_agent for plan in plans} == {expected["target_agent"]}
        assert {plan.intent for plan in plans} == {expected["intent"]}
        assert {plan.side_effect_policy for plan in plans} == {
            "draft_or_read_only"
        }
