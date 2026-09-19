from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.agent_mentions import parse_agent_mention
from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agents.orchestrator import OrchestratorPreflight, route_request
from keystone_agents.cli import (
    _interpreted_lifecycle_scope_text,
    _is_bounded_composite_lifecycle_request,
    _preflight_requires_work_item,
    _preflight_workflow_routes,
)
from keystone_agents.finance_expense_receipts import (
    resolve_finance_expense_receipt_target,
)
from keystone_agents.gmail_triage.execution_plan import resolve_gmail_execution_plan
from keystone_agents.manual_request import (
    infer_manual_request_plan,
    merge_manual_request_plan,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.work_item import WorkItem, WorkItemKind, WorkItemTarget
from keystone_agents.work_items import build_context_pack_for_route

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
        workflow=expected.get("workflow", []),
        intent=expected["intent"],
        provider_system=(
            expected["provider"]
            if expected["provider"]
            in {
                "airtable", "google_workspace", "gmail", "google_calendar",
                "zotero", "slack", "openai_vector_store",
            }
            else "unspecified"
        ),
        provider_operations=(
            expected["provider_operations"]
            if "provider_operations" in expected
            else ["create", "verify", "update", "verify", "delete", "verify"]
            if expected["target_agent"] in {"airtable_context_agent", "gmail_triage"}
            else ["create", "verify", "delete", "verify"]
            if expected["target_agent"] == "google_workspace_context_agent"
            else []
        ),
        provider_read_scope=expected.get("provider_read_scope", "unspecified"),
        provider_result_mode=expected.get("provider_result_mode", "unspecified"),
        gmail_mailbox_direction=expected.get(
            "gmail_mailbox_direction",
            "unspecified",
        ),
        gmail_date_scope=expected.get("gmail_date_scope", "unspecified"),
        gmail_requested_fields=expected.get("gmail_requested_fields", []),
        primary_target=expected["primary_target"],
        target_type=expected.get("target_type")
        or (
            "business_system_context" if expected["intent"] == "business_system_write" else "topic"
        ),
        objective=expected["objective"],
        task_objective=expected.get("task_objective")
        or (
            "business_system_write"
            if expected["intent"] == "business_system_write"
            else (
                "opportunity_discovery"
                if expected["intent"] == "opportunity_search"
                else "route_or_continue"
            )
        ),
        expected_artifact_type=expected.get("expected_artifact_type")
        or (
            "business_system_write_plan"
            if expected["intent"] == "business_system_write"
            else ("opportunity_record" if expected["intent"] == "opportunity_search" else "none")
        ),
        constraints=expected["constraints"],
        required_entities=expected.get("required_entities", []),
        gmail_query=expected.get("gmail_query", ""),
        requires_live_search=expected["provider"] == "shared_retrieval",
        requires_approved_context=expected.get(
            "requires_approved_context",
            False,
        ),
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


def test_semantic_routing_fixture_covers_every_executable_agent_family() -> None:
    """Keep semantic equivalence coverage aligned with the live agent registry."""

    covered_agents = {group["expected"]["target_agent"] for group in _payload()["groups"]}
    executable_agents = set(AGENT_REGISTRY) - {"orchestrator"}

    assert covered_agents == executable_agents


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
            assert plan.workflow == expected.get("workflow", []), variant
            assert plan.provider_read_scope == expected.get(
                "provider_read_scope",
                "unspecified",
            ), variant
            assert plan.provider_result_mode == expected.get(
                "provider_result_mode",
                "unspecified",
            ), variant
            assert plan.gmail_mailbox_direction == expected.get(
                "gmail_mailbox_direction",
                "unspecified",
            ), variant
            assert plan.gmail_date_scope == expected.get(
                "gmail_date_scope",
                "unspecified",
            ), variant


def test_equivalent_provider_lifecycles_reach_one_direct_owner_gate() -> None:
    for group in _payload()["groups"]:
        expected = group["expected"]
        if (
            expected["target_agent"] not in LIFECYCLE_ROUTES
            or expected["intent"] != "business_system_write"
            or "delete" not in expected.get("provider_operations", ["delete"])
        ):
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
                manual_plan=plan,
            ), variant


def test_equivalent_read_only_groups_preserve_owner_and_no_write_boundary() -> None:
    for group in _payload()["groups"]:
        expected = group["expected"]
        if expected["intent"] == "business_system_write":
            continue
        plans = [_interpreted_plan(variant["prompt"], expected) for variant in group["variants"]]

        assert {plan.target_agent for plan in plans} == {expected["target_agent"]}
        assert {plan.intent for plan in plans} == {expected["intent"]}
        assert {plan.side_effect_policy for plan in plans} == {"draft_or_read_only"}


def test_equivalent_gmail_reads_keep_one_provider_execution_contract() -> None:
    group = next(
        item for item in _payload()["groups"] if item["id"] == "gmail_selected_message_read"
    )
    plans = [
        resolve_gmail_execution_plan(
            variant["prompt"],
            manual_plan=_interpreted_plan(variant["prompt"], group["expected"]),
        )
        for variant in group["variants"]
    ]

    assert {plan.operation for plan in plans} == {"thread_summary"}
    assert {plan.gmail_query for plan in plans} == {'subject:"Product Leader"'}
    assert {plan.live_read_required for plan in plans} == {True}


def test_equivalent_known_contact_asks_keep_one_gmail_sdk_contract() -> None:
    group = next(
        item for item in _payload()["groups"] if item["id"] == "gmail_known_contact_lookup"
    )
    plans = [
        resolve_gmail_execution_plan(
            variant["prompt"],
            manual_plan=_interpreted_plan(variant["prompt"], group["expected"]),
        )
        for variant in group["variants"]
    ]

    assert {plan.operation for plan in plans} == {"contact_lookup"}
    assert {plan.gmail_query for plan in plans} == {'"Acme Compute"'}
    assert {plan.max_messages for plan in plans} == {10}
    assert {plan.side_effect_policy for plan in plans} == {"read_only"}
    assert {plan.live_read_required for plan in plans} == {True}


def test_equivalent_airtable_receipt_asks_keep_one_target_and_tool_shape() -> None:
    group = next(item for item in _payload()["groups"] if item["id"] == "airtable_personal_receipt")
    targets = [
        resolve_finance_expense_receipt_target(
            f"{variant['prompt']} /tmp/semantic-receipt.png",
            manual_plan=_interpreted_plan(variant["prompt"], group["expected"]),
        )
        for variant in group["variants"]
    ]

    assert all(target is not None for target in targets)
    assert {target.table for target in targets if target is not None} == {"Personal Expenses"}
    assert {target.operation for target in targets if target is not None} == {"create"}


def test_equivalent_multi_owner_asks_keep_one_graph_and_context_contract() -> None:
    group = next(
        item
        for item in _payload()["groups"]
        if item["id"] == "chief_supplied_evidence_multi_owner_review"
    )
    expected = group["expected"]

    for variant in group["variants"]:
        mention = parse_agent_mention(variant["prompt"])
        plan = _interpreted_plan(variant["prompt"], expected)
        route_result = route_request(mention.input_text, manual_plan=plan)
        preflight = OrchestratorPreflight(
            request_text=mention.input_text,
            requested_agent=mention.route if mention.explicit else None,
            advisory_only=True,
            selected_agent=plan.target_agent,
            manual_request_plan=plan,
            route_result=route_result,
        )

        assert plan.workflow == expected["workflow"], variant
        assert plan.requires_durable_state is True, variant
        assert _preflight_workflow_routes(preflight) == expected["workflow"], variant
        assert _preflight_requires_work_item(
            preflight,
            request_text=mention.input_text,
        ), variant

        work_item = WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Northstar Care supplied-evidence review",
            request_text=mention.input_text,
            target=WorkItemTarget(
                name=plan.primary_target,
                object_type=plan.target_type,
                metadata={
                    "manual_constraints": plan.constraints,
                    "manual_request_plan": plan.model_dump(mode="json"),
                },
            ),
        )
        packs = [
            build_context_pack_for_route(work_item, route)
            for route in expected["workflow"]
        ]

        assert all(pack.request_text == mention.input_text for pack in packs), variant
        assert all(
            set(expected["constraints"]) <= set(pack.constraints)
            for pack in packs
        ), variant
        assert all(
            pack.ask_shape == plan.ask_shape
            for pack in packs
        ), variant


def test_explicit_rag_variations_preserve_corpus_scope_without_live_retrieval() -> None:
    group = next(
        item for item in _payload()["groups"] if item["id"] == "rag_semantic_article_lookup"
    )
    for variant in group["variants"]:
        mention = parse_agent_mention(variant["prompt"])
        plan = _interpreted_plan(variant["prompt"], group["expected"])

        assert mention.explicit is True
        assert mention.route == "rag_retrieval_specialist"
        assert plan.provider_system == "openai_vector_store"
        assert plan.provider_operations == ["read"]
        assert plan.target_type == "vector_store_corpus"
        assert plan.task_objective == "corpus_retrieval"
        assert plan.expected_artifact_type == "rag_retrieval_result"
        assert plan.requires_live_search is False
        assert plan.side_effect_policy == "draft_or_read_only"
