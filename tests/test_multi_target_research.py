from __future__ import annotations

from typing import Any

from keystone_agents.agents.business_research_analyst import research_company_fixture
from keystone_agents.multi_target_research import (
    MultiTargetResearchPlan,
    build_multi_target_research_plan,
    candidate_targets_from_search_results,
    discover_candidate_targets,
    rank_candidate_targets,
    render_multi_target_research_summary,
    run_multi_target_research,
    should_run_multi_target_research,
)
from keystone_agents.quality_budget import AgentQualityBudget, QualityMode
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


def test_category_comparison_triggers_multi_target_branch() -> None:
    assert should_run_multi_target_research(
        request_text=(
            "Compare how three public AI companion products describe teen safety and escalation."
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
            "target_type": "topic",
            "desired_count": 3,
            "desired_count_explicit": True,
            "task_objective": "entity_research",
            "expected_artifact_type": "research_brief",
        },
        target="behavioral-health navigation vendors",
    )


def test_plan_preserves_explicit_company_as_comparison_anchor() -> None:
    plan = build_multi_target_research_plan(
        request_text="Compare Northstar with two competing workflow platforms.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Northstar",
            "target_type": "company",
            "desired_count": 2,
            "desired_count_explicit": True,
            "required_entities": ["Northstar"],
            "required_terms": ["audit trail"],
        },
        target="Northstar",
        cost_profile="standard",
    )

    assert plan.anchor_target == "Northstar"
    assert plan.desired_count == 2


def test_plan_does_not_invent_anchor_for_multiple_named_entities() -> None:
    plan = build_multi_target_research_plan(
        request_text="Compare Northstar, Beacon, and Harbor.",
        manual_plan={
            "source": "llm",
            "target_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": "Northstar",
            "target_type": "company",
            "desired_count": 3,
            "desired_count_explicit": True,
            "required_entities": ["Northstar", "Beacon", "Harbor"],
        },
        target="Northstar",
        cost_profile="standard",
    )

    assert plan.anchor_target == ""
    assert plan.desired_count == 3


def test_anchor_is_researched_first_excluded_from_candidates_and_rendered() -> None:
    plan = MultiTargetResearchPlan(
        topic="workflow platforms",
        anchor_target="Northstar",
        desired_count=2,
        requested_dimensions=["audit trail"],
        request_text="Compare Northstar with two competing workflow platforms.",
    )
    results_by_marker = {
        "competitors alternatives": [
            SearchResult(
                title="Northstar audit trail",
                link="https://northstar.example/audit",
                snippet="Northstar describes its audit trail.",
                source="searxng",
            ),
            SearchResult(
                title="Beacon audit trail",
                link="https://beacon.example/audit",
                snippet="Beacon describes its audit trail.",
                source="searxng",
            ),
            SearchResult(
                title="Harbor audit trail",
                link="https://harbor.example/audit",
                snippet="Harbor describes its audit trail.",
                source="searxng",
            ),
        ]
    }
    retrieval_order: list[str] = []
    events: list[str] = []

    class OrderedSearchProvider(_FakeSearchProvider):
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            events.append("candidate_search")
            return super().search_web(query, num_results)

    def ordered_provider_builder(**_kwargs: Any) -> OrderedSearchProvider:
        return OrderedSearchProvider(results_by_marker)

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        retrieval_order.append(company)
        events.append(f"profile:{company}")
        domain = company.lower().replace(" ", "")
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{domain}:audit",
                        f"{company} audit trail",
                        f"https://{domain}.example/audit",
                        f"{company} describes its audit trail.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        search_provider_builder=ordered_provider_builder,
        retrieve_profile=retrieve_profile,
    )
    rendered = render_multi_target_research_summary(result)
    from keystone_agents.workflow_runner import _multi_target_work_item_sources

    work_item_sources = _multi_target_work_item_sources(result)

    assert retrieval_order[0] == "Northstar"
    assert events[0] == "profile:Northstar"
    assert events.index("candidate_search") < events.index("profile:Beacon")
    assert events.index("candidate_search") < events.index("profile:Harbor")
    assert {candidate.name for candidate in result.candidate_targets} == {
        "Beacon",
        "Harbor",
    }
    assert result.selected_targets == ["Beacon", "Harbor"]
    assert result.anchor_packet is not None
    assert result.anchor_packet.target_name == "Northstar"
    assert [packet.target_name for packet in result.packets] == ["Beacon", "Harbor"]
    assert result.comparison_ready
    assert result.diagnostics["anchor_source_sufficient"] is True
    assert result.diagnostics["total_comparison_packet_count"] == 3
    assert result.anchor_packet.source_refs[0]["source_id"] == "northstar:audit"
    assert "| anchor | Northstar | sufficient |" in rendered
    assert {source.url for source in work_item_sources} == {
        "https://northstar.example/audit",
        "https://beacon.example/audit",
        "https://harbor.example/audit",
    }


def test_anchor_source_gap_blocks_otherwise_ready_competitor_comparison() -> None:
    plan = MultiTargetResearchPlan(
        topic="workflow platforms",
        anchor_target="Northstar",
        desired_count=2,
        requested_dimensions=["audit trail"],
        request_text="Compare Northstar with two competing workflow platforms.",
    )
    results_by_marker = {
        "competitors alternatives": [
            SearchResult(
                title="Northstar audit trail",
                link="https://northstar.example/audit",
                snippet="Northstar describes its audit trail.",
                source="searxng",
            ),
            SearchResult(
                title="Beacon audit trail",
                link="https://beacon.example/audit",
                snippet="Beacon describes its audit trail.",
                source="searxng",
            ),
            SearchResult(
                title="Harbor audit trail",
                link="https://harbor.example/audit",
                snippet="Harbor describes its audit trail.",
                source="searxng",
            ),
        ]
    }

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        sources = []
        if company != "Northstar":
            domain = company.lower()
            sources = [
                _source(
                    f"{domain}:audit",
                    f"{company} audit trail",
                    f"https://{domain}.example/audit",
                    f"{company} describes its audit trail.",
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
    assert result.diagnostics["ready_packet_count"] == 2
    assert result.diagnostics["anchor_source_sufficient"] is False
    assert any(blocker.startswith("Northstar:") for blocker in result.blockers)


def test_multi_target_uses_retrieval_max_results_quality_budget_field() -> None:
    plan = MultiTargetResearchPlan(
        topic="workflow platforms",
        desired_count=2,
        requested_dimensions=["audit trail"],
    )
    search_limits: list[int] = []
    profile_limits: list[int] = []
    profile_deadlines: list[int | None] = []
    search_results = [
        SearchResult(
            title="Beacon audit trail",
            link="https://beacon.example/audit",
            snippet="Beacon describes its audit trail.",
            source="searxng",
        ),
        SearchResult(
            title="Harbor audit trail",
            link="https://harbor.example/audit",
            snippet="Harbor describes its audit trail.",
            source="searxng",
        ),
    ]

    class RecordingSearchProvider:
        provider_name = "fake"

        def search_web(self, _query: str, num_results: int = 5) -> list[SearchResult]:
            search_limits.append(num_results)
            return search_results[:num_results]

    provider = RecordingSearchProvider()

    def provider_builder(**_kwargs: Any) -> RecordingSearchProvider:
        return provider

    def retrieve_profile(**kwargs: Any):
        company = str(kwargs["company"])
        profile_limits.append(int(kwargs["max_results"]))
        profile_deadlines.append(kwargs.get("retrieval_deadline_seconds"))
        domain = company.lower()
        profile = research_company_fixture(company_name=company).model_copy(
            update={
                "sources": [
                    _source(
                        f"{domain}:audit",
                        f"{company} audit trail",
                        f"https://{domain}.example/audit",
                        f"{company} describes its audit trail.",
                    )
                ]
            }
        )
        return profile, {"retrieval_diagnostics": {"provider_summary": "fake"}}

    result = run_multi_target_research(
        plan,
        live_search=True,
        quality_budget=AgentQualityBudget(
            agent_name="business_research_analyst",
            mode=QualityMode.FAST,
            retrieval_max_results=3,
            max_seconds=45,
        ),
        search_provider_builder=provider_builder,
        retrieve_profile=retrieve_profile,
    )

    assert result.comparison_ready
    assert search_limits and set(search_limits) == {3}
    assert profile_limits and set(profile_limits) == {3}
    assert profile_deadlines and set(profile_deadlines) == {45}


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
                snippet="Nomi AI public source mentions safety.",
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

    assert {"Character.AI", "Nomi", "Replika"} <= {candidate.name for candidate in candidates[:5]}
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
        "official safety pages": [
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

    def retrieve_profile(**kwargs: Any):
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
    selection = result.diagnostics["target_selection"]
    assert [item["name"] for item in selection["selected_targets"]] == [
        packet.target_name for packet in result.packets
    ]
    assert all(item["source_sufficient"] for item in selection["selected_targets"])
    assert selection["top_candidates"]


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
        "named products": [
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


def test_ai_companion_discovery_rejects_adjacent_teen_safety_products() -> None:
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
    assert "Core Ethos" not in names


def test_non_product_depth_sources_do_not_make_selected_products_sufficient() -> None:
    plan = MultiTargetResearchPlan(
        topic="three public AI companion or chatbot products",
        desired_count=3,
        requested_dimensions=["trusted contact", "teen safety", "escalation"],
        request_text="Compare three public AI companion or chatbot products.",
    )
    results_by_marker = {
        "named products": [
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
        "named products": [
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
        "named products": [
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
