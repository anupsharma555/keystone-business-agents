from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from time import perf_counter
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agents.business_research_analyst import research_company_fixture
from keystone_agents.multi_target_research import (
    CandidateTarget,
    MultiTargetResearchPlan,
    MultiTargetResearchResult,
    PerTargetResearchPacket,
    build_multi_target_research_plan,
    candidate_targets_from_search_results,
    discover_candidate_targets,
    rank_candidate_targets,
    render_multi_target_research_summary,
    run_multi_target_research,
    should_run_multi_target_research,
)
from keystone_agents.retrieval_policy import ProviderRequestBudget
from keystone_agents.runtime.provider_context import ProviderExecutionContext
from keystone_agents.schemas.company_profile import SourceRecord
from keystone_agents.tools.search_provider import SearchResult


class _FakeSearchProvider:
    provider_name = "fake"

    def __init__(self, results_by_marker: dict[str, list[SearchResult]]) -> None:
        self.results_by_marker = results_by_marker

    def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
        for marker, results in self.results_by_marker.items():
            if marker in query:
                return results[:num_results]
        return []


def _provider_builder(results_by_marker: dict[str, list[SearchResult]]):
    def build_provider(**_kwargs: Any) -> _FakeSearchProvider:
        return _FakeSearchProvider(results_by_marker)

    return build_provider


def _source(source_id: str, title: str, url: str, claim: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title=title,
        url=url,
        source_type="company_site",
        supported_claims=[claim] if claim else [],
        evidence_excerpt=claim,
        confidence=0.85,
    )


def _anchor_comparison_discovery_results() -> dict[str, list[SearchResult]]:
    return {
        '"Deliberate AI"': [
            SearchResult(
                title="Deliberate AI top competitors and alternatives",
                link=(
                    "https://www.cbinsights.com/research/report/"
                    "deliberate-ai-alternatives-competitors"
                ),
                snippet=(
                    "Deliberate AI's top competitors include Ellipsis Health "
                    "and BlueSkeye AI."
                ),
                source="agents-web-search",
            )
        ]
    }


def test_category_comparison_triggers_multi_target_branch() -> None:
    assert should_run_multi_target_research(
        request_text=(
            "Compare how three public AI companion products describe teen safety "
            "and escalation."
        ),
        manual_plan={
            "desired_count": 3,
            "planner_warnings": [
                "Target is a product category rather than a single named company."
            ],
        },
        target="public AI companion products",
    )


def test_single_company_deeper_comparison_does_not_trigger_multi_target_branch() -> None:
    assert not should_run_multi_target_research(
        request_text=(
            "Business research analyst do a deeper source-backed search on OpenAI "
            "mental health work and compare what the deeper lanes add."
        ),
        manual_plan={
            "intent": "company_research",
            "primary_target": "OpenAI",
        },
        target="OpenAI",
    )


def test_canonical_single_target_plan_is_not_broadened_by_comparison_prose() -> None:
    assert not should_run_multi_target_research(
        request_text="Compare three products, but answer only for the selected company.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Selected Health",
            "target_type": "company",
            "desired_count": 1,
            "desired_count_explicit": True,
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
        },
        target="Selected Health",
    )


def test_canonical_multi_target_plan_does_not_need_trigger_words_downstream() -> None:
    assert should_run_multi_target_research(
        request_text="Review the selected market scope.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "behavioral-health navigation vendors",
            "target_type": "company",
            "desired_count": 3,
            "desired_count_explicit": True,
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
        },
        target="behavioral-health navigation vendors",
    )


def test_open_entity_set_uses_typed_breadth_without_explicit_count() -> None:
    plan = {
        "source": "llm",
        "target_agent": "business_research_analyst",
        "intent": "company_research",
        "primary_target": "Deliberate AI",
        "target_type": "company",
        "provider_result_mode": "items",
        "desired_count": 1,
        "desired_count_explicit": False,
        "requires_target_discovery": True,
        "anchor_entity": "Deliberate AI",
        "task_objective": "entity_research",
        "expected_artifact_type": "research_brief",
        "required_entities": ["Deliberate AI"],
        "required_terms": ["multimodal", "behavioral health"],
        "ask_shape": {
            "ask_breadth": "broad",
            "evidence_depth": "deep",
            "stop_condition": "Stop after a credible source-backed entity set.",
        },
    }

    assert should_run_multi_target_research(
        request_text="Review the requested company landscape.",
        manual_plan=plan,
        target="Deliberate AI",
    )

    execution_plan = build_multi_target_research_plan(
        request_text="Review the requested company landscape.",
        manual_plan=plan,
        target="Deliberate AI",
        cost_profile="slack_research_deep",
    )

    assert execution_plan.desired_count == 3
    assert execution_plan.anchor_target == "Deliberate AI"
    assert execution_plan.anchor_source == "explicit"
    assert execution_plan.requested_dimensions == [
        "multimodal",
        "behavioral health",
    ]


def test_canonical_plan_does_not_reparse_count_or_dimensions_from_prose() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text=(
            "Compare five organizations on teen safety; this wording is not the "
            "canonical execution contract."
        ),
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "clinical workflow platforms",
            "target_type": "company",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "desired_count": 5,
            "desired_count_explicit": False,
            "desired_count_mode": "maximum",
            "desired_count_scope": "additional",
            "requires_target_discovery": True,
            "required_terms": [],
        },
        target="clinical workflow platforms",
        cost_profile="balanced",
    )

    assert execution_plan.desired_count == 3
    assert execution_plan.desired_count_mode == "unspecified"
    assert execution_plan.desired_count_scope == "unspecified"
    assert execution_plan.requested_dimensions == [
        "product or service overlap",
        "target-market overlap",
        "public source evidence",
    ]


def test_anchor_total_count_excludes_anchor_from_peer_goal() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text="Evaluate the typed comparison scope.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Anchor Health",
            "target_type": "company",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "desired_count": 5,
            "desired_count_explicit": True,
            "desired_count_mode": "target",
            "desired_count_scope": "total",
            "requires_target_discovery": True,
            "anchor_entity": "Anchor Health",
        },
        target="Anchor Health",
        cost_profile="balanced",
    )

    assert execution_plan.desired_count == 5
    assert execution_plan.desired_count_scope == "total"
    assert execution_plan.peer_goal == 4


def test_anchor_additional_count_preserves_full_peer_goal() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text="Evaluate the typed comparison scope.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Anchor Health",
            "target_type": "company",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "desired_count": 5,
            "desired_count_explicit": True,
            "desired_count_mode": "minimum",
            "desired_count_scope": "additional",
            "requires_target_discovery": True,
            "anchor_entity": "Anchor Health",
        },
        target="Anchor Health",
        cost_profile="balanced",
    )

    assert execution_plan.desired_count_mode == "minimum"
    assert execution_plan.desired_count_scope == "additional"
    assert execution_plan.peer_goal == 5


def test_maximum_count_underfill_requires_bounded_search_exhaustion() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health platforms",
        desired_count=3,
        desired_count_mode="maximum",
        requested_dimensions=["multimodal"],
    )
    packets = [
        PerTargetResearchPacket(
            target_name=name,
            source_sufficient=True,
            dimension_source_ids={"multimodal": [f"{name}:product"]},
        )
        for name in ("Alpha Health", "Beta Care")
    ]

    before_exhaustion = multi_target_research._multi_target_readiness(
        plan,
        anchor_packet=None,
        packets=packets,
        bounded_search_exhausted=False,
    )
    after_exhaustion = multi_target_research._multi_target_readiness(
        plan,
        anchor_packet=None,
        packets=packets,
        bounded_search_exhausted=True,
    )

    assert before_exhaustion.count_contract_satisfied is False
    assert before_exhaustion.whole_request_ready is False
    assert after_exhaustion.count_contract_satisfied is True
    assert after_exhaustion.whole_request_ready is True


def test_nonmaximum_count_modes_do_not_accept_exhausted_underfill() -> None:
    from keystone_agents import multi_target_research

    packet = PerTargetResearchPacket(
        target_name="Alpha Health",
        source_sufficient=True,
        dimension_source_ids={"multimodal": ["alpha:product"]},
    )
    for mode in ("target", "minimum", "exact"):
        readiness = multi_target_research._multi_target_readiness(
            MultiTargetResearchPlan(
                topic="multimodal behavioral-health platforms",
                desired_count=2,
                desired_count_mode=mode,
                requested_dimensions=["multimodal"],
            ),
            anchor_packet=None,
            packets=[packet],
            bounded_search_exhausted=True,
        )
        assert readiness.count_contract_satisfied is False, mode
        assert readiness.whole_request_ready is False, mode


def test_offline_maximum_plan_does_not_claim_bounded_search_exhaustion() -> None:
    result = run_multi_target_research(
        MultiTargetResearchPlan(
            topic="multimodal behavioral-health platforms",
            desired_count=3,
            desired_count_mode="maximum",
            requested_dimensions=["multimodal"],
        ),
        live_search=False,
    )

    assert result.readiness.bounded_search_exhausted is False
    assert result.readiness.count_contract_satisfied is False
    assert result.readiness.whole_request_ready is False


def test_empty_live_provider_cannot_complete_maximum_multi_target_request() -> None:
    result = run_multi_target_research(
        MultiTargetResearchPlan(
            topic="multimodal behavioral-health platforms",
            desired_count=3,
            desired_count_mode="maximum",
            requested_dimensions=["multimodal"],
        ),
        live_search=True,
        search_provider_builder=_provider_builder({}),
        retrieve_profile=lambda **_kwargs: pytest.fail(
            "no target profile should run without a discovered target"
        ),
    )

    assert result.selected_targets == []
    assert result.readiness.whole_request_ready is False
    assert result.request_coverage.status == "blocked"
    assert result.blockers
    assert result.pass_types[-1] != "final_synthesis"


def test_open_entity_set_preserves_legacy_single_entity_anchor() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text="Review related organizations.",
        manual_plan={
            "source": "llm",
            "primary_target": "expanded market phrase",
            "requires_target_discovery": True,
            "required_entities": ["Legacy Anchor"],
            "required_terms": ["clinical workflow"],
        },
        target="expanded market phrase",
        cost_profile="balanced",
    )

    assert execution_plan.anchor_target == "Legacy Anchor"
    assert execution_plan.anchor_source == "legacy_single_entity"


def test_open_entity_set_preserves_exact_seed_over_expanded_primary_target() -> None:
    from keystone_agents import multi_target_research

    execution_plan = build_multi_target_research_plan(
        request_text="Find multimodal behavioral-health competitors to Deliberate AI.",
        manual_plan={
            "source": "llm",
            "primary_target": (
                "Deliberate AI and competitors in multimodal behavioral health"
            ),
            "required_entities": ["Deliberate AI"],
            "requires_target_discovery": True,
            "required_terms": ["competitors", "multimodal", "behavioral health"],
        },
        target="Deliberate AI and competitors in multimodal behavioral health",
        cost_profile="slack_research_deep",
    )

    assert execution_plan.topic == "Deliberate AI"
    assert all(
        "Deliberate AI and competitors in" not in query
        for query in multi_target_research._candidate_discovery_queries(execution_plan)
    )
    assert any(
        query.startswith('"Deliberate AI"')
        for query in multi_target_research._candidate_discovery_queries(execution_plan)
    )


def test_explicit_entity_count_remains_the_multi_target_execution_count() -> None:
    plan = build_multi_target_research_plan(
        request_text="Review the selected category.",
        manual_plan={
            "source": "llm",
            "primary_target": "behavioral-health AI companies",
            "desired_count": 5,
            "desired_count_explicit": True,
            "required_terms": ["multimodal", "behavioral health"],
        },
        target="behavioral-health AI companies",
        cost_profile="slack_research_deep",
    )

    assert plan.desired_count == 5


def test_generic_broad_single_company_plan_does_not_trigger_target_discovery() -> None:
    assert not should_run_multi_target_research(
        request_text="Deeply profile OpenAI using source-backed evidence.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "OpenAI",
            "target_type": "company",
            "provider_result_mode": "items",
            "desired_count": 5,
            "desired_count_explicit": False,
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "constraints": ["source-backed"],
            "ask_shape": {"ask_breadth": "broad", "evidence_depth": "deep"},
        },
        target="OpenAI",
    )


def test_generic_candidate_queries_use_requested_dimensions_without_safety_leakage() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["competitor", "multimodal", "behavioral health"],
        request_text="Review the requested company landscape.",
    )

    queries = multi_target_research._candidate_discovery_queries(plan)

    assert any("multimodal" in query and "behavioral health" in query for query in queries)
    assert all("teen safety" not in query for query in queries)
    assert all("trusted contact" not in query for query in queries)


def test_anchor_discovery_includes_a_rubric_only_category_lane() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Example Anchor",
        anchor_target="Example Anchor",
        desired_count=3,
        requested_dimensions=[
            "behavioral-health",
            "multiple data modalities",
            "official source",
        ],
        request_text="Find three related products with official sources.",
    )

    queries = multi_target_research._candidate_discovery_queries(plan)

    assert any(
        "Example Anchor" not in query
        and "behavioral-health" in query
        and "multiple data modalities" in query
        for query in queries
    )
    assert all("official source" not in query for query in queries)


def test_enterprise_chatbot_queries_do_not_inject_companion_product_language() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="enterprise customer service chatbot platforms",
        desired_count=3,
        requested_dimensions=["customer service", "enterprise integration"],
        request_text="Find up to three enterprise customer-service chatbot platforms.",
    )

    queries = multi_target_research._candidate_discovery_queries(plan)

    assert all("ChatGPT" not in query for query in queries)
    assert all("Replika" not in query for query in queries)
    assert all("teen safety" not in query for query in queries)
    assert all("trusted contact" not in query for query in queries)


def test_openai_official_domain_resolves_company_identity_not_product_identity() -> None:
    plan = MultiTargetResearchPlan(
        topic="enterprise AI platform companies",
        desired_count=3,
        requested_dimensions=["enterprise", "AI platform"],
    )

    candidates = candidate_targets_from_search_results(
        [
            SearchResult(
                title="About OpenAI",
                link="https://openai.com/about/",
                snippet="OpenAI develops AI platforms for enterprise and research use.",
                source="searxng",
            )
        ],
        plan=plan,
    )

    assert [candidate.name for candidate in candidates] == ["OpenAI"]


def test_official_domain_identity_preserves_published_title_capitalization() -> None:
    plan = MultiTargetResearchPlan(
        topic="behavioral-health assessment companies",
        desired_count=2,
        requested_dimensions=["behavioral health"],
    )

    candidates = candidate_targets_from_search_results(
        [
            SearchResult(
                title="CareSage behavioral-health assessment",
                link="https://caresage.com/",
                snippet="CareSage develops behavioral-health assessment tools.",
                source="exa",
            )
        ],
        plan=plan,
    )

    assert [candidate.name for candidate in candidates] == ["CareSage"]


@pytest.mark.parametrize("relationship_first", [False, True])
def test_official_domain_and_relationship_result_share_one_ai_brand_identity(
    relationship_first: bool,
) -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Callyope",
        anchor_target="Callyope",
        desired_count=3,
        requested_dimensions=["competitors", "behavioral health"],
    )
    official = SearchResult(
        title="BlueSkeye AI | Behavioral-health measurement",
        link="https://blueskeye.com/",
        snippet="BlueSkeye AI uses multimodal signals in behavioral health.",
        source="exa",
    )
    relationship = SearchResult(
        title="Top Callyope competitors",
        link="https://example.test/callyope-competitors",
        snippet="Callyope competitors include BlueSkeye AI.",
        source="searxng",
    )
    search_results = (
        [relationship, official]
        if relationship_first
        else [official, relationship]
    )

    candidates = candidate_targets_from_search_results(search_results, plan=plan)

    matches = [
        candidate
        for candidate in candidates
        if candidate.normalized_key in {"blueskeye", "blueskeyeai"}
    ]
    assert [(candidate.name, len(candidate.evidence)) for candidate in matches] == [
        ("BlueSkeye", 2)
    ]
    candidate = matches[0]
    assert candidate.normalized_key == "blueskeye"
    assert multi_target_research._candidate_has_relationship_evidence(
        candidate,
        plan=plan,
    )
    profile = research_company_fixture(company_name="BlueSkeye").model_copy(
        update={
            "sources": [
                _source(
                    "blueskeye:official",
                    "BlueSkeye behavioral-health measurement",
                    "https://blueskeye.com/",
                    "BlueSkeye uses multimodal signals in behavioral health.",
                )
            ]
        }
    )
    packet = multi_target_research._packet_from_profile(
        plan,
        candidate,
        profile,
        {},
    )
    assert packet.source_sufficient
    assert "anchor relationship remains to be qualified" not in packet.gaps


@pytest.mark.parametrize(
    "company,url",
    [
        ("Acme Labs", "https://acme-labs.notion.site/product"),
        ("Beta Care", "https://beta-care.github.io/clinical-ai"),
        ("Gamma Health", "https://gamma-health.substack.com/p/product"),
    ],
)
def test_hosted_tenant_page_is_not_company_owned_official_evidence(
    company: str,
    url: str,
) -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="behavioral-health AI companies",
        desired_count=2,
        requested_dimensions=["behavioral health", "multiple data modalities"],
    )
    candidate = CandidateTarget(
        name=company,
        normalized_key=multi_target_research._candidate_key(company),
    )
    profile = research_company_fixture(company_name=company).model_copy(
        update={
            "sources": [
                _source(
                    f"{company}:hosted",
                    f"{company} behavioral-health product",
                    url,
                    (
                        f"{company} combines voice and language signals for "
                        "behavioral-health assessment."
                    ),
                )
            ]
        }
    )

    packet = multi_target_research._packet_from_profile(
        plan,
        candidate,
        profile,
        {},
    )

    assert not packet.source_sufficient
    assert "missing target-specific official/product source" in packet.gaps


def test_competitor_packet_keeps_unresolved_anchor_relationship_as_a_blocker() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Callyope",
        anchor_target="Callyope",
        desired_count=2,
        requested_dimensions=[
            "competitors",
            "behavioral health",
            "multiple data modalities",
        ],
        request_text=(
            "Compare Callyope with competitors using multiple data modalities "
            "in behavioral health."
        ),
    )
    candidate = rank_candidate_targets(
        candidate_targets_from_search_results(
            [
                SearchResult(
                    title="CareSage behavioral-health measurement",
                    link="https://caresage.com/",
                    snippet=(
                        "CareSage combines voice and language signals for "
                        "behavioral-health assessment."
                    ),
                    source="exa",
                )
            ],
            plan=plan,
        ),
        plan=plan,
    )[0]
    profile = research_company_fixture(company_name="CareSage").model_copy(
        update={
            "sources": [
                _source(
                    "caresage:official",
                    "CareSage behavioral-health measurement",
                    "https://caresage.com/",
                    (
                        "CareSage combines voice and language signals for "
                        "behavioral-health assessment."
                    ),
                )
            ]
        }
    )

    packet = multi_target_research._packet_from_profile(
        plan,
        candidate,
        profile,
        {},
    )
    readiness = multi_target_research._multi_target_readiness(
        plan,
        anchor_packet=PerTargetResearchPacket(
            target_name="Callyope",
            source_sufficient=True,
            dimension_source_ids={
                "behavioral health": ["anchor:official"],
                "multiple data modalities": ["anchor:official"],
            },
        ),
        packets=[packet],
    )

    assert not packet.source_sufficient
    assert "anchor relationship remains to be qualified" in packet.gaps
    assert not readiness.whole_request_ready


def test_unrelated_official_site_is_not_admitted_without_typed_relevance() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        anchor_target="Deliberate AI",
        desired_count=3,
        requested_dimensions=["multimodal", "behavioral health"],
    )
    candidates = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Ada health assessment services",
                link="https://ada.com/about/",
                snippet="Ada provides general health assessment services.",
                source="searxng",
            )
        ],
        plan=plan,
    )

    assert rank_candidate_targets(candidates, plan=plan) == []


def test_seed_company_is_not_returned_as_its_own_related_target() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["multimodal", "behavioral health"],
    )

    candidates = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Deliberate AI and Limbic compared",
                link="https://deliberate.ai/research",
                snippet="Deliberate AI and Limbic build behavioral-health AI products.",
                source="searxng",
            ),
            SearchResult(
                title="Limbic clinical AI",
                link="https://limbic.ai/",
                snippet="Limbic describes behavioral-health clinical AI.",
                source="searxng",
            ),
        ],
        plan=plan,
    )

    names = {candidate.name for candidate in candidates}
    assert "Deliberate AI" not in names
    assert "Limbic" in names


def test_parallel_discovery_keeps_candidates_from_productive_lane() -> None:
    plan = MultiTargetResearchPlan(
        topic="public AI companion products",
        desired_count=3,
        requested_dimensions=["teen safety", "escalation", "trusted contact"],
        request_text="Compare three public AI companion products.",
    )
    results_by_marker = {
        "official products": [
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety controls.",
                source="searxng",
            ),
            SearchResult(
                title="Character.AI safety center",
                link="https://character.ai/safety",
                snippet="Character.AI describes teen safety and escalation.",
                source="searxng",
            ),
                SearchResult(
                    title="Nomi AI safety",
                    link="https://nomi.ai/safety",
                    snippet="Nomi describes its public AI companion product and safety controls.",
                    source="searxng",
                ),
        ],
        "comparison": [],
        "teen safety": [],
        "public safety": [],
    }

    candidates = discover_candidate_targets(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
    )

    assert {"Character.AI", "Nomi", "Replika"} <= {
        candidate.name for candidate in candidates[:5]
    }
    assert all(candidate.ranking_score > 0 for candidate in candidates[:3])


def test_balanced_budget_runs_one_breadth_repair_pass() -> None:
    plan = MultiTargetResearchPlan(
        topic="public AI companion products",
        desired_count=3,
        requested_dimensions=["teen safety"],
        request_text="Compare three public AI companion products.",
    )
    results_by_marker = {
        "official products": [],
        "comparison": [],
        "teen safety escalation": [],
        "public safety policy": [],
        "named organizations": [
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety.",
                source="searxng",
            ),
            SearchResult(
                title="Character.AI safety center",
                link="https://character.ai/safety",
                snippet="Character.AI describes teen safety.",
                source="searxng",
            ),
            SearchResult(
                title="Nomi AI safety",
                link="https://nomi.ai/safety",
                snippet="Nomi AI describes teen safety.",
                source="searxng",
            ),
        ],
    }

    retrieval_calls: list[dict[str, Any]] = []

    def retrieve_profile(**kwargs: Any):
        retrieval_calls.append(kwargs)
        company = str(kwargs["company"])
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:safety",
                        f"{company} safety",
                        f"https://{company.lower().replace('.', '').replace(' ', '')}.example",
                        f"{company} describes teen safety.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready
    assert "candidate_breadth_repair" in result.pass_types
    assert result.diagnostics["candidate_count_after_initial"] == 0
    assert retrieval_calls
    assert all(call["max_queries"] == 4 for call in retrieval_calls)
    selection = result.diagnostics["target_selection"]
    assert [item["name"] for item in selection["selected_targets"]] == [
        packet.target_name for packet in result.packets
    ]
    assert all(item["source_sufficient"] for item in selection["selected_targets"])
    assert selection["top_candidates"]


def test_anchor_relative_research_profiles_anchor_once_and_keeps_it_out_of_peers() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        anchor_target="Deliberate AI",
        anchor_source="explicit",
        desired_count=2,
        requested_dimensions=["multimodal", "behavioral health"],
        request_text=(
            "Deeply research Deliberate AI, then identify similar multimodal "
            "behavioral-health companies."
        ),
    )
    retrieval_calls: list[dict[str, Any]] = []
    phase_events: list[str] = []

    def retrieve_profile(**kwargs: Any):
        retrieval_calls.append(kwargs)
        company = str(kwargs["company"])
        phase_events.append(f"depth:{company}")
        company_key = company.lower().replace(" ", "").replace(".", "")
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company_key}:product",
                        f"{company} product",
                        f"https://{company_key}.example/product",
                        (
                            f"{company} uses multimodal signals in behavioral "
                            "health applications."
                        ),
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    class RecordingSearchProvider(_FakeSearchProvider):
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            phase_events.append("candidate_discovery")
            return super().search_web(query, num_results)

    def build_provider(**_kwargs: Any) -> RecordingSearchProvider:
        return RecordingSearchProvider(_anchor_comparison_discovery_results())

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=build_provider,
        retrieve_profile=retrieve_profile,
    )

    companies = [str(call["company"]) for call in retrieval_calls]
    assert companies.count("Deliberate AI") == 1
    assert set(result.selected_targets) == {"Ellipsis Health", "BlueSkeye AI"}
    assert "Deliberate AI" not in result.selected_targets
    assert result.anchor_packet is not None
    assert result.anchor_packet.target_name == "Deliberate AI"
    assert result.comparison_ready
    assert result.readiness.whole_request_ready
    assert result.request_coverage.status == "complete"
    assert phase_events[0] == "depth:Deliberate AI"
    discovery_index = phase_events.index("candidate_discovery")
    assert discovery_index > 0
    assert all(
        phase_events.index(f"depth:{company}") > discovery_index
        for company in ("Ellipsis Health", "BlueSkeye AI")
    )
    assert next(
        call["max_queries"]
        for call in retrieval_calls
        if call["company"] == "Deliberate AI"
    ) == 3
    assert all(
        call["max_queries"] == 4
        for call in retrieval_calls
        if call["company"] != "Deliberate AI"
    )


def test_insufficient_anchor_fails_before_candidate_or_peer_retrieval() -> None:
    plan = MultiTargetResearchPlan(
        topic="Anchor Co",
        anchor_target="Anchor Co",
        anchor_source="explicit",
        desired_count=2,
        requested_dimensions=["voice biomarkers", "behavioral health"],
        request_text="Research Anchor Co, then identify comparable companies.",
    )
    retrieval_calls: list[str] = []

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        retrieval_calls.append(company)
        return research_company_fixture(company_name=company).model_copy(
            update={"sources": []}
        ), {"retrieval_diagnostics": {"provider_summary": "fake"}}

    def provider_must_not_construct(**_kwargs: Any) -> _FakeSearchProvider:
        raise AssertionError("candidate discovery must wait for a ready anchor")

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=provider_must_not_construct,
        retrieve_profile=retrieve_profile,
    )

    assert retrieval_calls == ["Anchor Co", "Anchor Co"]
    assert result.candidate_targets == []
    assert result.packets == []
    assert result.comparison_ready is False
    assert result.diagnostics["next_action"] == "repair_anchor_research"
    assert result.diagnostics["anchor_evidence_gap"]["anchor"] is True
    assert result.diagnostics["anchor_evidence_gap"]["missing_official_source"] is True
    assert result.pass_types == ["anchor_depth", "repair_anchor_research"]
    assert any("repair_anchor_research" in blocker for blocker in result.blockers)


def test_anchor_evidence_gap_drives_one_repair_before_candidate_discovery() -> None:
    plan = MultiTargetResearchPlan(
        topic="Anchor Co",
        anchor_target="Anchor Co",
        anchor_source="explicit",
        desired_count=2,
        desired_count_scope="total",
        requested_dimensions=["voice biomarkers", "behavioral health"],
        request_text="Research Anchor Co, then identify comparable companies.",
    )
    retrieval_calls: list[tuple[str, str]] = []

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        request_text = str(kwargs["request_text"])
        retrieval_calls.append((company, request_text))
        if company == "Anchor Co" and not request_text.startswith("Repair"):
            sources = []
        else:
            key = company.lower().replace(" ", "")
            relationship = (
                " and is comparable with Anchor Co"
                if company != "Anchor Co"
                else ""
            )
            sources = [
                _source(
                    f"{key}:product",
                    f"{company} product",
                    f"https://{key}.example/product",
                    (
                        f"{company} uses voice biomarkers in behavioral health"
                        f"{relationship}."
                    ),
                )
            ]
        return research_company_fixture(company_name=company).model_copy(
            update={"sources": sources}
        ), {
            "retrieval_diagnostics": {
                "provider_summary": "fake",
                "query_result_count": 1 if sources else 0,
            }
        }

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(
            {
                '"Anchor Co"': [
                    SearchResult(
                        title="Anchor Co competitors include Callyope",
                        link="https://example.test/anchor-co-competitors",
                        snippet="Callyope is a comparable behavioral-health voice AI company.",
                        source="fake",
                    )
                ]
            }
        ),
        retrieve_profile=retrieve_profile,
    )

    anchor_calls = [call for call in retrieval_calls if call[0] == "Anchor Co"]
    assert len(anchor_calls) == 2
    assert anchor_calls[1][1].startswith("Repair the source evidence")
    assert "repair_anchor_research" in result.pass_types
    assert result.anchor_packet is not None
    assert result.anchor_packet.source_sufficient is True
    assert result.candidate_targets


def test_fixed_named_targets_skip_discovery_and_research_exact_set() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text="Evaluate the selected organizations on the requested dimensions.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Alpha Health and Beta Care",
            "target_type": "topic",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "required_entities": ["Alpha Health", "Beta Care"],
            "required_terms": ["behavioral health", "multimodal"],
            "requires_target_discovery": False,
            "desired_count": 3,
            "desired_count_explicit": False,
        },
        target="Alpha Health and Beta Care",
        cost_profile="balanced",
    )
    retrieval_calls: list[str] = []

    def provider_builder(**_kwargs: Any) -> _FakeSearchProvider:
        raise AssertionError("fixed target execution must not call discovery")

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        retrieval_calls.append(company)
        key = company.lower().replace(" ", "")
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{key}:product",
                        f"{company} product",
                        f"https://{key}.example/product",
                        (
                            f"{company} combines multimodal inputs for behavioral "
                            "health applications."
                        ),
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        execution_plan,
        live_search=True,
        search_provider_builder=provider_builder,
        retrieve_profile=retrieve_profile,
    )

    assert execution_plan.fixed_targets == ["Alpha Health", "Beta Care"]
    assert execution_plan.desired_count == 2
    assert set(retrieval_calls) == {"Alpha Health", "Beta Care"}
    assert result.selected_targets == ["Alpha Health", "Beta Care"]
    assert result.readiness.whole_request_ready
    assert result.diagnostics["candidate_discovery_query_count"] == 0
    assert "fixed_target_set" in result.pass_types
    assert "target_substitution" not in result.pass_types


def test_fixed_named_targets_block_conflicting_explicit_count() -> None:
    execution_plan = build_multi_target_research_plan(
        request_text="Evaluate the selected organizations.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Alpha Health and Beta Care",
            "target_type": "topic",
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
            "required_entities": ["Alpha Health", "Beta Care"],
            "requires_target_discovery": False,
            "desired_count": 3,
            "desired_count_explicit": True,
        },
        target="Alpha Health and Beta Care",
        cost_profile="balanced",
    )

    assert execution_plan.desired_count == 2
    assert execution_plan.planning_blockers == [
        "explicit count 3 conflicts with the 2 named comparison targets"
    ]


def test_invalid_canonical_plan_cannot_reopen_multi_target_phrase_inference() -> None:
    with pytest.raises(
        ValueError,
        match="invalid manual request plan cannot build multi-target research",
    ):
        build_multi_target_research_plan(
            request_text="Compare Alpha Health and Beta Care.",
            manual_plan={
                "source": "llm",
                "target_agent": "business_research_analyst",
                "intent": "company_research",
                "target_type": "company_collection",
            },
            target="Alpha Health and Beta Care",
            cost_profile="balanced",
        )


def test_ready_peers_do_not_complete_when_anchor_evidence_is_missing() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        anchor_target="Deliberate AI",
        anchor_source="explicit",
        desired_count=2,
        requested_dimensions=["multimodal", "behavioral health"],
        request_text=(
            "Deeply research Deliberate AI, then identify similar multimodal "
            "behavioral-health companies."
        ),
    )

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        company_key = company.lower().replace(" ", "").replace(".", "")
        sources = (
            []
            if company == "Deliberate AI"
            else [
                _source(
                    f"{company_key}:product",
                    f"{company} product",
                    f"https://{company_key}.example/product",
                    (
                        f"{company} uses multimodal signals in behavioral "
                        "health applications."
                    ),
                )
            ]
        )
        profile = research_company_fixture(company_name=company).model_copy(
            update={"sources": sources}
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(
            _anchor_comparison_discovery_results()
        ),
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready is False
    assert result.readiness.peer_comparison_ready is False
    assert not result.readiness.anchor_ready
    assert not result.readiness.whole_request_ready
    assert result.request_coverage.status == "blocked"
    assert result.candidate_targets == []
    assert result.packets == []
    assert result.diagnostics["candidate_discovery_query_count"] == 0
    assert result.diagnostics["next_action"] == "repair_anchor_research"
    assert any("anchor gap for Deliberate AI" in blocker for blocker in result.blockers)


def test_whole_request_requires_every_substantive_rubric_dimension() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        anchor_target="Deliberate AI",
        anchor_source="explicit",
        desired_count=2,
        requested_dimensions=["competitor", "multimodal", "behavioral health"],
    )
    packet = PerTargetResearchPacket(
        target_name="Candidate",
        source_sufficient=True,
        dimension_source_ids={
            "multimodal": ["candidate:product"],
            "behavioral health": [],
        },
    )
    from keystone_agents import multi_target_research

    readiness = multi_target_research._multi_target_readiness(
        plan,
        anchor_packet=packet.model_copy(update={"target_name": "Deliberate AI"}),
        packets=[packet, packet.model_copy(update={"target_name": "Candidate 2"})],
    )

    assert readiness.rubric_dimensions == ["multimodal", "behavioral health"]
    assert readiness.peer_comparison_ready
    assert not readiness.whole_request_ready
    assert readiness.rubric_ready_peer_count == 0
    assert any("behavioral health" in gap for gap in readiness.gaps)


def test_live_like_multimodal_anchor_evidence_satisfies_semantic_rubric() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Callyope",
        anchor_target="Callyope",
        anchor_source="explicit",
        desired_count=3,
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health",
            "clinical decision support",
            "official source",
        ],
        request_text=(
            "Find products using multiple data modalities for behavioral-health "
            "measurement or clinical decision support."
        ),
    )
    claim = (
        "Callyope uses voice and language together with clinical history, sleep "
        "data, and activity levels to assess and continuously monitor mental health "
        "symptoms for clinicians."
    )
    profile = research_company_fixture(company_name="Callyope").model_copy(
        update={
            "sources": [
                _source(
                    "callyope:official",
                    "Callyope Clinical AI for Mental Health Care",
                    "https://www.callyope.com/",
                    claim,
                )
            ]
        }
    )
    candidate = CandidateTarget(
        name="Callyope",
        normalized_key="callyope",
        url_candidates=["https://www.callyope.com/"],
    )

    packet = multi_target_research._packet_from_profile(
        plan,
        candidate,
        profile,
        {"retrieval_diagnostics": {}},
    )

    assert packet.source_sufficient
    assert packet.gaps == []
    assert all(packet.dimension_source_ids.values())


def test_explicit_or_dimensions_form_alternative_rubric_lanes() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health platforms",
        desired_count=2,
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health measurement",
            "clinical decision support",
        ],
        request_text=(
            "Find products using multiple data modalities for behavioral-health "
            "measurement or clinical decision support."
        ),
    )
    packet = PerTargetResearchPacket(
        target_name="Candidate A",
        source_sufficient=True,
        dimension_source_ids={
            "multiple data modalities": ["candidate-a:official"],
            "behavioral-health measurement": ["candidate-a:official"],
            "clinical decision support": [],
        },
    )

    readiness = multi_target_research._multi_target_readiness(
        plan,
        anchor_packet=None,
        packets=[
            packet,
            packet.model_copy(update={"target_name": "Candidate B"}),
        ],
        bounded_search_exhausted=True,
    )

    assert readiness.rubric_ready_peer_count == 2
    assert readiness.count_contract_satisfied
    assert readiness.whole_request_ready


def test_candidate_ledger_dedupes_across_sources_and_preserves_evidence() -> None:
    plan = MultiTargetResearchPlan(
        topic="public AI companion products",
        desired_count=3,
        requested_dimensions=["teen safety"],
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Replika vs Character.AI vs Candy AI",
                link="https://companionrank.com/replika-vs-character-ai-candy-ai",
                snippet="A listicle names Replika, Character.AI, and Candy AI.",
                source="searxng",
            ),
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika explains teen safety.",
                source="agents-web-search",
            ),
        ],
        plan=plan,
    )
    ranked = rank_candidate_targets(ledger, plan=plan)
    replika = next(candidate for candidate in ranked if candidate.name == "Replika")

    assert len(replika.evidence) == 2
    assert replika.url_candidates == ["https://replika.com/safety"]
    assert "Replika" in [candidate.name for candidate in ranked]
    assert "Character.AI" in [candidate.name for candidate in ranked]


def test_discovery_filters_source_organizations_from_bad_live_run() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["teen safety", "trusted contact", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="OpenAI to alert trusted contact if it detects potential self-harm risk",
                link="https://www.business-standard.com/technology/openai-trusted-contact.html",
                snippet="OpenAI describes trusted contact features for ChatGPT.",
                source="searxng",
            ),
            SearchResult(
                title="Common Sense Media launches Youth AI Safety Institute",
                link="https://www.commonsensemedia.org/articles/youth-ai-safety",
                snippet="Common Sense Media comments on teen safety and chatbots.",
                source="searxng",
            ),
            SearchResult(
                title="Brookings contact page",
                link="https://www.brookings.edu/contact-brookings/",
                snippet="Contact Brookings for policy research.",
                source="searxng",
            ),
            SearchResult(
                title="Trusted contacts in ChatGPT",
                link="https://help.openai.com/en/articles/20001105-trusted-contacts-in-chatgpt",
                snippet="ChatGPT lets users name a trusted contact.",
                source="agents-web-search",
            ),
        ],
        plan=plan,
    )
    names = {candidate.name for candidate in ledger}

    assert "ChatGPT" in names
    assert "Citizen" not in names
    assert "Commonsensemedia" not in names
    assert "Brookings" not in names


def test_discovery_rejects_directory_and_publisher_brands_as_competitors() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["competitors", "multimodal", "behavioral health"],
        request_text="Find multimodal behavioral-health competitors to Deliberate AI.",
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Top AI in Mental Health Companies - Fortune Business Insights",
                link=(
                    "https://www.fortunebusinessinsights.com/blog/"
                    "top-ai-in-mental-health-companies-11306"
                ),
                snippet=(
                    "Cognoa is a specialized provider of pediatric behavioral-health "
                    "solutions."
                ),
                source="searxng",
            ),
            SearchResult(
                title="Deliberate | B Corp - Product Alternatives",
                link=(
                    "https://marketplace.aviahealth.com/product/99487/"
                    "deliberate-b-corp-product/competitors/alternatives"
                ),
                snippet="Marketplace profile of Deliberate and its alternatives.",
                source="agents-web-search",
            ),
            SearchResult(
                title="Deliberate AI 2026 Company Profile | PitchBook",
                link="https://pitchbook.com/profiles/company/461962-81",
                snippet="Deliberate AI company profile, valuation, funding, and investors.",
                source="searxng",
            ),
            SearchResult(
                title="Limbic clinical AI",
                link="https://limbic.ai/",
                snippet="Limbic provides behavioral-health clinical AI.",
                source="searxng",
            ),
            SearchResult(
                title="Deliberate | B Corp: Products",
                link="https://marketplace.aviahealth.com/company/99484",
                snippet=(
                    "AVIA marketplace page summarizing Deliberate's ambient "
                    "multimodal AI."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="AI Admissions & Intake for Behavioral Health - BH AI Landscape",
                link="https://behavioralhealthadmissions.com/",
                snippet=(
                    "Behavioral health AI landscape page covering intake and "
                    "admissions tools."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="Top Deliberate AI Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/deliberate-ai/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Competitor overview listing Deliberate AI's top competitors "
                    "such as Ellipsis Health, BlueSkeye AI, and Kintsugi."
                ),
                source="agents-web-search",
            ),
        ],
        plan=plan,
    )
    names = {candidate.name for candidate in ledger}
    ranked_names = [
        candidate.name for candidate in rank_candidate_targets(ledger, plan=plan)
    ]

    assert "Limbic" in names
    assert "Cognoa" not in names
    assert "Fortunebusinessinsights" not in names
    assert "Aviahealth" not in names
    assert "Marketplace" not in names
    assert "Pitchbook" not in names
    assert "B Corp" not in names
    assert "Company Profile" not in names
    assert "Funding" not in names
    assert "Investors" not in names
    assert "AVIA" not in names
    assert "Behavioralhealthadmissions" not in names
    assert {"Ellipsis Health", "BlueSkeye AI", "Kintsugi"} <= names
    assert {"Ellipsis Health", "BlueSkeye AI", "Kintsugi"} <= set(ranked_names[:6])


def test_discovery_admits_company_domain_when_product_title_uses_a_different_brand() -> None:
    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health clinical decision support",
        anchor_target="Anchor Health",
        desired_count=3,
        desired_count_mode="exact",
        desired_count_scope="additional",
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health measurement",
            "clinical decision support",
        ],
        source_constraints=["official sources"],
        request_text=(
            "Compare Anchor Health with exactly three companies whose official "
            "product pages support multimodal behavioral-health decision support."
        ),
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title=(
                    "Prism Platform | Multimodal Clinical AI by "
                    "SignalSpring Health"
                ),
                link="https://signalspringhealth.example/platform",
                snippet=(
                    "Prism combines Voice AI, computer vision, and speech biomarkers "
                    "for behavioral-health screening and clinical decision support."
                ),
                source="exa",
            ),
            SearchResult(
                title="Prism Clinical Intelligence by SignalSpring Health",
                link=(
                    "https://signalspringhealth.example/"
                    "clinical-intelligence"
                ),
                snippet=(
                    "Voice, language, vision, and behavioral signals support "
                    "clinical screening and decision support."
                ),
                source="exa",
            ),
            SearchResult(
                title="Top Anchor Health Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/anchor-health/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Anchor Health competitors include HealthArc and OOTify."
                ),
                source="agents-web-search",
            ),
        ],
        plan=plan,
        result_query_by_url={
            "https://signalspringhealth.example/platform": (
                "multimodal behavioral health clinical AI official"
            ),
            (
                "https://signalspringhealth.example/"
                "clinical-intelligence"
            ): "behavioral health clinical decision support official",
            (
                "https://www.cbinsights.com/company/anchor-health/"
                "alternatives-competitors"
            ): "Anchor Health competitors",
        },
    )

    signal_spring = next(
        candidate
        for candidate in ledger
        if candidate.name == "SignalSpring Health"
    )
    assert signal_spring.url_candidates == [
        "https://signalspringhealth.example/platform",
        "https://signalspringhealth.example/clinical-intelligence",
    ]
    assert len(signal_spring.evidence) == 2
    assert all(candidate.name != "Prism Platform" for candidate in ledger)
    ranked = rank_candidate_targets(ledger, plan=plan)
    assert ranked[0].name == "SignalSpring Health"
    assert ranked[0].ranking_score > max(
        (
            candidate.ranking_score
            for candidate in ranked
            if candidate.name in {"HealthArc", "OOTify"}
        ),
        default=0,
    )


def test_discovery_keeps_official_company_behind_distinct_product_title() -> None:
    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health clinical decision support",
        anchor_target="Anchor Health",
        desired_count=3,
        desired_count_mode="exact",
        desired_count_scope="additional",
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health measurement",
            "clinical decision support",
        ],
        source_constraints=["official sources"],
        request_text=(
            "Compare Anchor Health with exactly three companies whose official "
            "product pages support multimodal behavioral-health decision support."
        ),
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Maaind | Multimodal mental health AI by Scienza Health",
                link="https://scienzahealth.com/maaind",
                snippet=(
                    "Maaind combines voice, language, and behavioral signals for "
                    "mental-health assessment and clinical decision support."
                ),
                source="exa",
            )
        ],
        plan=plan,
        result_query_by_url={
            "https://scienzahealth.com/maaind": (
                "multimodal behavioral health clinical AI official"
            )
        },
    )

    assert [candidate.name for candidate in ledger] == ["Scienza Health"]
    assert ledger[0].url_candidates == ["https://scienzahealth.com/maaind"]


def test_discovery_does_not_promote_product_directory_domain_as_company() -> None:
    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health clinical decision support",
        anchor_target="Anchor Health",
        desired_count=3,
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health measurement",
            "clinical decision support",
        ],
        request_text=(
            "Find companies using multiple data modalities for behavioral-health "
            "clinical decision support."
        ),
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Acme Behavioral Health AI",
                link="https://vendorhub.example/product/acme",
                snippet=(
                    "Acme combines voice, language, and behavioral signals for "
                    "behavioral-health clinical decision support."
                ),
                source="exa",
            )
        ],
        plan=plan,
    )

    assert all(candidate.name != "Vendorhub" for candidate in ledger)


@pytest.mark.parametrize(
    ("title", "url", "expected_owner"),
    [
        (
            "Prism Platform | Multimodal Clinical AI by SignalSpring Health",
            "https://signalspringhealth.example/product/prism",
            "SignalSpring Health",
        ),
        (
            "Maaind | Multimodal mental health AI by Scienza Health",
            "https://scienzahealth.com/product/maaind",
            "Scienza Health",
        ),
    ],
)
def test_product_path_keeps_normalized_explicit_owner(
    title: str,
    url: str,
    expected_owner: str,
) -> None:
    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health clinical decision support",
        anchor_target="Anchor Health",
        desired_count=3,
        requested_dimensions=[
            "multiple data modalities",
            "behavioral-health measurement",
            "clinical decision support",
        ],
        request_text=(
            "Find companies using multiple data modalities for behavioral-health "
            "clinical decision support."
        ),
    )

    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title=title,
                link=url,
                snippet=(
                    "The product combines voice, language, and behavioral signals "
                    "for clinical decision support."
                ),
                source="exa",
            )
        ],
        plan=plan,
    )

    assert [candidate.name for candidate in ledger] == [expected_owner]


def test_relationship_evidence_ranks_ahead_of_unverified_official_candidates() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["competitors", "multimodal", "behavioral health"],
        request_text="Find multimodal behavioral-health competitors to Deliberate AI.",
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Maaind · Multimodal psychophysiology",
                link="https://www.maaind.com/",
                snippet=(
                    "Official vendor page described by hosted search as a competitor "
                    "using multimodal signals in behavioral health."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="About Ada",
                link="https://about.ada.com/about-us/about-ada/",
                snippet=(
                    "Official company page described by hosted search as an adjacent "
                    "competitor in behavioral-health AI."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="Top Deliberate AI Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/deliberate-ai/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Competitor overview listing Deliberate AI alternatives including "
                    "Ellipsis Health, BlueSkeye AI, Kintsugi, and Modality.AI."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="Deliberate AI - Products, Competitors, Financials",
                link="https://www.cbinsights.com/company/deliberate-ai",
                snippet=(
                    "Deliberate AI is named with challengers including Ellipsis Health, "
                    "Health Rhythms, and Behavidence."
                ),
                source="searxng",
            ),
            SearchResult(
                title="Top MentalHealth Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/mentalhealthcom/"
                    "alternatives-competitors"
                ),
                snippet="MentalHealth competitors include Aiberry and Flock Health.",
                source="searxng",
            ),
        ],
        plan=plan,
    )

    ranked_names = [
        candidate.name for candidate in rank_candidate_targets(ledger, plan=plan)
    ]

    assert {"Ellipsis Health", "BlueSkeye AI", "Kintsugi"} <= set(ranked_names[:5])
    assert "Maaind" in ranked_names
    assert "Ada" in ranked_names
    assert ranked_names.index("Maaind") > ranked_names.index("Kintsugi")
    assert ranked_names.index("Ada") > ranked_names.index("Kintsugi")
    assert "Aiberry" not in ranked_names
    assert "Flock Health" not in ranked_names


def test_depth_pool_reserves_repair_for_official_rubric_match() -> None:
    from keystone_agents import multi_target_research

    weak_relationships = [
        CandidateTarget(
            name=name,
            normalized_key=name.lower(),
            ranking_score=70,
            gaps=["candidate evidence does not mention requested dimensions"],
        )
        for name in ("HealthArc", "OOTify", "PulseLife", "Sonde", "VOS")
    ]
    official_match = CandidateTarget(
        name="Mirah",
        normalized_key="mirah",
        ranking_score=68,
        url_candidates=["https://www.mirah.example/"],
        gaps=["anchor relationship remains to be qualified"],
    )

    pool = multi_target_research._depth_candidate_pool(
        [*weak_relationships, official_match],
        peer_goal=3,
    )

    assert [candidate.name for candidate in pool[:3]] == [
        "HealthArc",
        "OOTify",
        "PulseLife",
    ]
    assert pool[3].name == "Mirah"
    assert len(pool) == 5


def test_anchor_linked_relationships_rank_ahead_of_other_company_lists() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["competitors", "multimodal", "behavioral health"],
        request_text="Find multimodal behavioral-health competitors to Deliberate AI.",
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Top Deliberate AI Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/deliberate-ai/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Deliberate AI's top competitors include Ellipsis Health, "
                    "BlueSkeye AI, and Kintsugi."
                ),
                source="searxng",
            ),
            SearchResult(
                title="Top Kintsugi Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/kintsugi/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Kintsugi's top competitors include Callyope and OPTT Health. "
                    "The same page later mentions Deliberate AI and multimodal "
                    "behavioral health."
                ),
                source="exa",
            ),
            SearchResult(
                title="Top Ellipsis Health Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/ellipsis-health/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Ellipsis Health's top competitors include Hyro and PatientPoint. "
                    "The same page later mentions Deliberate AI."
                ),
                source="exa",
            ),
        ],
        plan=plan,
    )

    ranked_names = [
        candidate.name for candidate in rank_candidate_targets(ledger, plan=plan)
    ]

    assert {"Ellipsis Health", "BlueSkeye AI", "Kintsugi"} <= set(ranked_names)
    anchor_linked = max(
        ranked_names.index(name)
        for name in ("Ellipsis Health", "BlueSkeye AI", "Kintsugi")
    )
    for adjacent in ("Callyope", "Hyro", "PatientPoint"):
        if adjacent in ranked_names:
            assert ranked_names.index(adjacent) > anchor_linked


def test_directory_subject_outweighs_incidental_capitalized_snippet_terms() -> None:
    plan = MultiTargetResearchPlan(
        topic="Callyope",
        anchor_target="Callyope",
        desired_count=3,
        requested_dimensions=[
            "behavioral-health",
            "clinical decision support",
            "multiple data modalities",
            "official source",
        ],
        request_text=(
            "Research Callyope and identify three competitors using multiple "
            "data modalities for behavioral-health clinical decision support."
        ),
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Callyope uses Google AI to detect mental health symptoms",
                link="https://www.linkedin.com/posts/example",
                snippet=(
                    "Meet Callyope, a French startup leveraging Google AI. Learn "
                    "more through the Growth Academy."
                ),
                source="searxng",
            ),
            SearchResult(
                title="Top Kintsugi Alternatives, Competitors",
                link=(
                    "https://www.cbinsights.com/company/kintsugi/"
                    "alternatives-competitors"
                ),
                snippet=(
                    "Kintsugi's top competitors include Callyope and other "
                    "behavioral-health clinical AI companies."
                ),
                source="exa",
            ),
        ],
        plan=plan,
    )

    ranked_names = [
        candidate.name for candidate in rank_candidate_targets(ledger, plan=plan)
    ]

    assert "Kintsugi" in ranked_names
    assert "French" not in ranked_names
    assert "Google AI" not in ranked_names
    assert "Growth Academy" not in ranked_names
    assert "Learn" not in ranked_names


def test_full_research_uses_rubric_lane_instead_of_weak_anchor_snippet_entities() -> None:
    plan = MultiTargetResearchPlan(
        topic="Callyope",
        anchor_target="Callyope",
        desired_count=3,
        desired_count_mode="exact",
        desired_count_scope="additional",
        requested_dimensions=[
            "behavioral-health",
            "multiple data modalities",
            "official source",
        ],
        request_text=(
            "Deeply research Callyope, then identify exactly three additional "
            "companies using multiple data modalities in behavioral health. "
            "Require an official source for each company."
        ),
    )
    results_by_marker = {
        '"Callyope"': [
            SearchResult(
                title="Callyope uses Example Cloud AI",
                link="https://www.linkedin.com/posts/example",
                snippet=(
                    "Meet Callyope, a French startup using Example Cloud AI. "
                    "Learn more through the Growth Academy."
                ),
                source="searxng",
            )
        ],
        "companies products official": [
            SearchResult(
                title="Kintsugi behavioral-health AI",
                link="https://kintsugi.com/",
                snippet=(
                    "Kintsugi combines voice and language signals for "
                    "behavioral-health assessment."
                ),
                source="exa",
            ),
            SearchResult(
                title="Ellipsis Health behavioral-health AI",
                link="https://ellipsishealth.com/",
                snippet=(
                    "Ellipsis Health combines speech and clinical-history signals "
                    "for behavioral-health assessment."
                ),
                source="exa",
            ),
            SearchResult(
                title="BlueSkeye behavioral-health AI",
                link="https://blueskeye.com/",
                snippet=(
                    "BlueSkeye combines video and patient-reported signals for "
                    "behavioral-health assessment."
                ),
                source="exa",
            ),
        ],
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        domains = {
            "Callyope": "callyope.com",
            "Kintsugi": "kintsugi.com",
            "Ellipsis Health": "ellipsishealth.com",
            "BlueSkeye": "blueskeye.com",
        }
        modality_claims = {
            "Callyope": "Callyope combines voice and language signals.",
            "Kintsugi": "Kintsugi combines voice and language signals.",
            "Ellipsis Health": (
                "Ellipsis Health combines speech and clinical-history signals."
            ),
            "BlueSkeye": (
                "BlueSkeye combines video and patient-reported signals."
            ),
        }
        claim = (
            f"{modality_claims[company]} The product supports "
            "behavioral-health assessment."
        )
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:official",
                        f"{company} official product",
                        f"https://{domains[company]}/",
                        claim,
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready, (
        result.selected_targets,
        result.blockers,
        [
            (packet.target_name, packet.dimension_source_ids, packet.gaps)
            for packet in result.packets
        ],
    )
    assert result.readiness.whole_request_ready
    assert result.selected_targets == ["BlueSkeye", "Ellipsis Health", "Kintsugi"]
    assert all(packet.source_sufficient for packet in result.packets)
    candidate_names = {candidate.name for candidate in result.candidate_targets}
    assert {"French", "Example Cloud AI", "Growth Academy", "Learn"}.isdisjoint(
        candidate_names
    )


def test_candidate_discovery_quality_deepens_when_only_adjacent_official_sites_exist() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["competitors", "multimodal", "behavioral health"],
        request_text="Find multimodal behavioral-health competitors to Deliberate AI.",
    )
    assessment = multi_target_research._candidate_discovery_quality(
        [
            SearchResult(
                title="Lily behavioral-health platform",
                link="https://www.asklily.health/",
                snippet=(
                    "Lily provides telehealth, scheduling, notes, and billing for "
                    "behavioral-health organizations."
                ),
                source="agents-web-search",
            ),
            SearchResult(
                title="Navix Health",
                link="https://www.navixhealth.com/",
                snippet="Navix provides an AI-enabled behavioral-health EMR.",
                source="agents-web-search",
            ),
        ],
        plan,
    )

    assert assessment.needs_precision_search
    assert "too few candidate targets discovered" in assessment.reasons


def test_partial_multi_target_summary_answers_before_limitations() -> None:
    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=2,
        requested_dimensions=["multimodal", "behavioral health"],
    )
    result = MultiTargetResearchResult(
        plan=plan,
        candidate_targets=[],
        packets=[
            PerTargetResearchPacket(
                target_name="Limbic",
                extraction_status="extracted",
                gaps=["missing independent corroboration"],
            ),
            PerTargetResearchPacket(
                target_name="Ksana Health",
                extraction_status="extracted",
                gaps=["direct competition not yet established"],
            ),
        ],
        comparison_ready=False,
        blockers=["depth gap: 0/2 target packet(s) have sufficient source evidence"],
    )

    rendered = render_multi_target_research_summary(result)

    assert (
        "The strongest current candidates are Limbic, Ksana Health." in rendered
    )
    assert rendered.index("Answer") < rendered.index("Blockers")


def test_generic_listicle_seeds_candidate_but_not_sufficient_feature_evidence() -> None:
    plan = MultiTargetResearchPlan(
        topic="public AI companion products",
        desired_count=3,
        requested_dimensions=["teen safety", "escalation"],
        request_text="Compare three companion products on teen safety and escalation.",
    )
    results_by_marker = {
        "official products": [
            SearchResult(
                title="Replika vs Character.AI vs Candy AI",
                link="https://companionrank.com/replika-vs-character-ai-candy-ai",
                snippet="A generic ranking names the big three.",
                source="searxng",
            ),
            SearchResult(
                title="Nomi AI safety",
                link="https://nomi.ai/safety",
                snippet="Nomi describes teen safety.",
                source="searxng",
            ),
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety.",
                source="searxng",
            ),
            SearchResult(
                title="Character.AI safety",
                link="https://character.ai/safety",
                snippet="Character.AI describes escalation and teen safety.",
                source="searxng",
            ),
        ]
    }
    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        claim = (
            f"{company} public page describes teen safety and escalation."
            if company != "Candy AI"
            else "A generic ranking names Candy AI without feature evidence."
        )
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:safety",
                        f"{company} safety",
                        (
                            "https://"
                            f"{company.lower().replace('.', '').replace(' ', '')}"
                            ".example/safety"
                        ),
                        claim,
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready
    assert len(result.packets) == 3
    assert "Candy AI" in [candidate.name for candidate in result.candidate_targets]
    assert all(packet.target_name != "Candy AI" for packet in result.packets)
    assert all(packet.source_sufficient for packet in result.packets)


def test_unrelated_contact_or_safety_pages_do_not_pass_source_sufficiency() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["trusted contact", "teen safety", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )
    results_by_marker = {
        "organizations products official": [
            SearchResult(
                title="Brookings contact page",
                link="https://www.brookings.edu/contact-brookings/",
                snippet="Contact Brookings for policy research.",
                source="searxng",
            ),
            SearchResult(
                title="QBS safety care training",
                link="https://qbs.com/safety-care-training/",
                snippet="Training content includes safety language.",
                source="searxng",
            ),
            SearchResult(
                title="Common Sense Media teen safety",
                link="https://www.commonsensemedia.org/articles/teen-safety",
                snippet="Consumer guidance about teen safety.",
                source="searxng",
            ),
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:contact",
                        f"{company} contact",
                        f"https://{company.lower().replace(' ', '')}.example/contact",
                        "Contact page with generic safety wording.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert not result.comparison_ready
    assert any("breadth gap" in blocker for blocker in result.blockers)


def test_multi_target_blocks_with_depth_gap_when_sources_are_thin() -> None:
    plan = MultiTargetResearchPlan(
        topic="public AI companion products",
        desired_count=3,
        requested_dimensions=["trusted contact"],
        request_text="Compare three companion products on trusted contact.",
    )
    results_by_marker = {
        "official products": [
            SearchResult(
                title="Replika",
                link="https://replika.com/",
                snippet="Replika homepage.",
                source="searxng",
            ),
            SearchResult(
                title="Character.AI",
                link="https://character.ai/",
                snippet="Character.AI homepage.",
                source="searxng",
            ),
            SearchResult(
                title="Nomi AI",
                link="https://nomi.ai/",
                snippet="Nomi AI homepage.",
                source="searxng",
            ),
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:home",
                        f"{company} homepage",
                        f"https://{company.lower().replace('.', '').replace(' ', '')}.example/",
                        "Generic homepage copy without requested feature evidence.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert not result.comparison_ready
    assert any("depth gap" in blocker for blocker in result.blockers)
    assert all(
        "missing target-specific requested feature evidence" in packet.gaps
        for packet in result.packets
    )


def test_media_and_regulatory_domains_do_not_count_as_product_targets() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["trusted contact", "teen safety", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="AI companion chatbots unsafe for kids report",
                link="https://www.cnn.com/2025/04/30/tech/ai-companion-chatbots-unsafe-for-kids-report",
                snippet="CNN reports on AI companion chatbot teen safety concerns.",
                source="searxng",
            ),
            SearchResult(
                title="FTC launches inquiry into AI chatbots acting as companions",
                link=(
                    "https://www.ftc.gov/news-events/news/press-releases/2025/09/"
                    "ftc-launches-inquiry-ai-chatbots-acting-companions"
                ),
                snippet="FTC inquiry mentions AI companion chatbots and teen safety.",
                source="searxng",
            ),
            SearchResult(
                title="Meta halts teens access to AI characters",
                link=(
                    "https://www.facebook.com/VivaLanka/posts/"
                    "meta-halts-teens-access-to-aimeta-temporarily-suspends-teens-access"
                ),
                snippet="Facebook post mentions teen access to AI characters.",
                source="searxng",
            ),
            SearchResult(
                title="OpenAI launches new online safety feature",
                link=(
                    "https://apnews.com/article/"
                    "openai-chatgpt-chatbot-ai-online-safety-1e7169772a24147b4c04d13c76700aeb"
                ),
                snippet="AP reports on ChatGPT chatbot online safety.",
                source="searxng",
            ),
            SearchResult(
                title="ChatGPT safety protections",
                link="https://chatgpt.com/parent-resources/safety-protections/",
                snippet="ChatGPT describes teen safety protections.",
                source="searxng",
            ),
        ],
        plan=plan,
    )
    ranked = rank_candidate_targets(ledger, plan=plan)

    names = {candidate.name for candidate in ranked}
    assert "ChatGPT" in names
    assert "CNN" not in names
    assert "Ftc" not in names
    assert "Facebook" not in names
    assert "Apnews" not in names


def test_generic_discovery_keeps_novel_candidates_for_depth_qualification() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["trusted contact", "teen safety", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )
    ledger = candidate_targets_from_search_results(
        [
            SearchResult(
                title="Nudge | Trust-Preserving Teen Safety by CoreEthos",
                link="https://core-ethos.com/",
                snippet=(
                    "Create a family account, designate trusted adults, detect red-flag "
                    "patterns, and send trusted adult alerts."
                ),
                source="searxng",
            ),
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety for its AI companion.",
                source="searxng",
            ),
        ],
        plan=plan,
    )
    ranked = rank_candidate_targets(ledger, plan=plan)

    names = {candidate.name for candidate in ranked}
    assert "Replika" in names
    assert "Core Ethos" in names
    assert all(
        "AI companion/chatbot product topic" not in gap
        for candidate in ranked
        for gap in candidate.gaps
    )


def test_non_product_depth_sources_do_not_make_selected_products_sufficient() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["trusted contact", "teen safety", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )
    results_by_marker = {
        "organizations products official": [
            SearchResult(
                title="ChatGPT safety protections",
                link="https://chatgpt.com/parent-resources/safety-protections/",
                snippet="ChatGPT describes teen safety protections.",
                source="searxng",
            ),
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety protections.",
                source="searxng",
            ),
            SearchResult(
                title="Nomi AI safety",
                link="https://nomi.ai/safety",
                snippet="Nomi AI describes teen safety protections.",
                source="searxng",
            ),
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        if company == "ChatGPT":
            sources = [
                _source(
                    "chatgpt:safety",
                    "ChatGPT safety protections",
                    "https://chatgpt.com/parent-resources/safety-protections/",
                    "ChatGPT describes teen safety protections and escalation.",
                )
            ]
        elif company == "Replika":
            sources = [
                _source(
                    "cnn:report",
                    "AI companion chatbots unsafe for kids report",
                    "https://www.cnn.com/2025/04/30/tech/ai-companion-chatbots-unsafe-for-kids-report",
                    "CNN reports on AI companion chatbot teen safety concerns.",
                )
            ]
        else:
            sources = [
                _source(
                    "facebook:post",
                    "Meta halts teens access to AI characters",
                    (
                        "https://www.facebook.com/VivaLanka/posts/"
                        "meta-halts-teens-access-to-aimeta-temporarily-suspends-teens-access"
                    ),
                    "Facebook post mentions AI companion chatbot teen safety and escalation.",
                )
            ]
        profile = research_company_fixture(company_name=company).model_copy(
            update={"sources": sources}
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert not result.comparison_ready
    assert result.diagnostics["ready_packet_count"] == 1
    selection = result.diagnostics["target_selection"]
    assert {item["name"] for item in selection["weak_targets"]} == {"Replika", "Nomi"}
    assert any("depth gap" in blocker for blocker in result.blockers)
    assert any(
        "missing target-specific official/product source" in packet.gaps
        for packet in result.packets
        if packet.target_name in {"Replika", "Nomi"}
    )


def test_depth_packet_preserves_selected_candidate_name_when_profile_drifts() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=2,
        requested_dimensions=["teen safety"],
        request_text="Compare two public AI companion products.",
    )
    results_by_marker = {
        "organizations products official": [
            SearchResult(
                title="ChatGPT safety protections",
                link="https://chatgpt.com/parent-resources/safety-protections/",
                snippet="ChatGPT describes teen safety protections.",
                source="searxng",
            ),
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety protections.",
                source="searxng",
            )
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        profile_name = "CNN" if company == "ChatGPT" else company
        domain = "chatgpt.com" if company == "ChatGPT" else "replika.com"
        profile = research_company_fixture(company_name=profile_name).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:safety",
                        f"{company} safety protections",
                        f"https://{domain}/safety",
                        f"{company} describes teen safety protections.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready
    assert result.packets[0].target_name == "ChatGPT"


def test_depth_packet_prioritizes_target_feature_sources_before_contact_pages() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=2,
        requested_dimensions=["teen safety", "escalation"],
        request_text="Compare two public AI companion products.",
    )
    results_by_marker = {
        "organizations products official": [
            SearchResult(
                title="Replika safety",
                link="https://replika.com/safety",
                snippet="Replika describes teen safety and escalation.",
                source="searxng",
            ),
            SearchResult(
                title="ChatGPT safety protections",
                link="https://chatgpt.com/parent-resources/safety-protections/",
                snippet="ChatGPT describes teen safety protections and escalation.",
                source="searxng",
            ),
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        if company == "Replika":
            sources = [
                _source(
                    "replika:contact",
                    "Contact Support - Replika",
                    "https://help.replika.com/hc/en-us/articles/115001095832-Contact-Support",
                    "Official support article explaining how to contact Replika support.",
                ),
                _source(
                    "replika:crisis",
                    "Can Replika help me if I'm in crisis?",
                    (
                        "https://help.replika.com/hc/en-us/articles/"
                        "360022375711-Can-Replika-help-me-if-I-m-in-crisis"
                    ),
                    (
                        "Official crisis-safety guidance directing users to emergency "
                        "services and trusted hotlines; relevant to escalation."
                    ),
                ),
            ]
        else:
            sources = [
                _source(
                    "chatgpt:safety",
                    "ChatGPT safety protections",
                    "https://chatgpt.com/parent-resources/safety-protections/",
                    "ChatGPT describes teen safety protections and escalation.",
                )
            ]
        profile = research_company_fixture(company_name=company).model_copy(
            update={"sources": sources}
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=_provider_builder(results_by_marker),
        retrieve_profile=retrieve_profile,
    )

    replika = next(packet for packet in result.packets if packet.target_name == "Replika")
    assert result.comparison_ready
    assert replika.source_refs[0]["source_id"] == "replika:crisis"


def test_search_snippet_claim_is_not_promoted_to_read_evidence() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="Deliberate AI",
        desired_count=3,
        requested_dimensions=["multimodal", "behavioral health"],
        request_text="Compare related multimodal behavioral-health companies.",
    )
    candidate = CandidateTarget(
        name="Example Health",
        normalized_key="examplehealth",
        url_candidates=["https://example.test/"],
    )
    profile = research_company_fixture(company_name="Example Health").model_copy(
        update={
            "sources": [
                SourceRecord(
                    source_id="search:example",
                    title="Example Health search result",
                    url="https://example.test/",
                    source_type="company_site",
                    supported_claims=[
                        "A search snippet mentions multimodal behavioral health."
                    ],
                    evidence_excerpt="",
                    confidence=0.7,
                )
            ]
        }
    )

    packet = multi_target_research._packet_from_profile(plan, candidate, profile, {})

    assert packet.source_sufficient is False
    assert packet.source_refs == []
    assert "snippet-only or unextracted source packet" in packet.gaps


@pytest.mark.parametrize(
    "url",
    [
        "https://alphareviews.example/products/alpha",
        "https://notalpha.example/alpha",
        "https://industry.example/reviews/alpha",
    ],
)
def test_third_party_domains_cannot_satisfy_official_target_source(
    url: str,
) -> None:
    from keystone_agents import multi_target_research

    assert (
        multi_target_research._looks_like_official_target_url(url, "alpha")
        is False
    )
    assert (
        multi_target_research._looks_like_official_target_url(
            "https://alpha.example/product",
            "alpha",
        )
        is True
    )


def test_provider_cap_prevents_multi_target_maximum_underfill_completion() -> None:
    from keystone_agents import multi_target_research

    plan = MultiTargetResearchPlan(
        topic="multimodal behavioral-health AI companies",
        desired_count=3,
        desired_count_mode="maximum",
        desired_count_scope="total",
        requested_dimensions=["multimodal"],
        request_text="Find up to three multimodal behavioral-health AI companies.",
    )
    results = {
        "organizations products official": [
            SearchResult(
                title="Example Health multimodal platform",
                link="https://examplehealth.ai/product",
                snippet="Example Health uses multimodal signals.",
                source="fake",
            )
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{company}:product",
                        f"{company} product",
                        "https://examplehealth.ai/product",
                        f"{company} uses multimodal behavioral signals.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    executor = ThreadPoolExecutor(max_workers=2)
    now = perf_counter()
    context = ProviderExecutionContext(
        settings=SimpleNamespace(search_provider="fake"),
        provider_config=SimpleNamespace(
            provider_sequence=("fake",),
            deepening_provider_sequence=(),
            parallel_provider_fanout=False,
            provider_request_budget=ProviderRequestBudget({"*": 1}),
        ),
        network_semaphore=BoundedSemaphore(2),
        provider_executor=executor,
        started_at=now,
        deadline_at=now + 30,
        runtime_metadata={},
    )
    try:
        result = multi_target_research._run_multi_target_research_in_context(
            plan,
            live_search=True,
            quality_budget=None,
            agents_web_search_max_calls=None,
            retrieval_hint=None,
            search_provider_builder=_provider_builder(results),
            retrieve_profile=retrieve_profile,
            execution_context=context,
        )
    finally:
        executor.shutdown(wait=True)

    receipt = result.diagnostics["bounded_search_receipt"]
    assert receipt["provider_attempt_count"] == 1
    assert receipt["query_attempt_count"] == receipt["planned_attempt_count"]
    assert receipt["query_ledger_valid"] is True
    assert receipt["provider_completed"] is False
    assert receipt["budget_or_deadline_stopped"] is True
    assert receipt["exhausted"] is False
    assert result.readiness.bounded_search_exhausted is False
    assert result.readiness.whole_request_ready is False
    assert result.request_coverage.status == "partial"
