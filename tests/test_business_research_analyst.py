from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_comparison_agent,
    build_business_research_analyst_focused_brief_agent,
    build_business_research_analyst_research_brief_agent,
    build_company_research_queries,
    build_source_bundle_for_synthesis,
    compare_company_profiles_for_decision,
    comparison_input_from_result,
    dedupe_and_rank_sources,
    focused_brief_input_from_profile,
    load_contact_context,
    load_crm_account_context,
    research_account_from_search_results,
    synthesize_company_profile_from_source_bundle,
)
from keystone_agents.company_research import (
    company_profile_markdown,
    company_research_comparison_markdown,
    research_company_fixture,
)
from keystone_agents.models import ResearchSDKInput
from keystone_agents.reporting import render_company_focused_brief
from keystone_agents.schemas.company_profile import (
    DEFAULT_RESEARCH_DATA_POINT_KEYS,
    CompanyBriefContactCandidate,
    CompanyBriefFact,
    CompanyBriefSourceCitation,
    CompanyProfile,
    CompanyResearchComparison,
    CompanyResearchFocusedBrief,
)
from keystone_agents.schemas.research import (
    ResearchArticleSummary,
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)
from keystone_agents.tools.serper_tool import SearchResult, search_web

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _comparison_fixture_profiles() -> tuple[CompanyProfile, CompanyProfile]:
    company_a = research_company_fixture(
        company_name="Signal Trial",
        fixture_json={
            "name": "Signal Trial",
            "website": "https://www.signaltrial.example",
            "description": "Clinical trial workflow company.",
            "fit": (
                "Signal Trial appears relevant to Keystone because it operates in "
                "clinical trial workflow software with validation and implementation needs."
            ),
            "sources": [
                {
                    "source_id": "company:about",
                    "title": "Signal Trial About",
                    "url": "https://www.signaltrial.example/about",
                    "source_type": "website",
                    "supported_claims": [
                        (
                            "Signal Trial builds clinical trial workflow software for "
                            "evidence generation."
                        ),
                        "Signal Trial supports validation studies and research operations teams.",
                    ],
                },
                {
                    "source_id": "news:partnership",
                    "title": "Signal Trial partnership",
                    "url": "https://www.reuters.com/example/signal-trial-partnership",
                    "source_type": "news",
                    "published_at": "2026-03-01",
                    "supported_claims": [
                        "Signal Trial announced a clinical validation partnership with a sponsor."
                    ],
                },
                {
                    "source_id": "linkedin:signaltrial",
                    "title": "Signal Trial LinkedIn",
                    "url": "https://www.linkedin.com/company/signaltrial",
                    "source_type": "linkedin",
                    "supported_claims": [
                        "Signal Trial serves clinical operations and research teams."
                    ],
                },
            ],
        },
    )
    company_b = research_company_fixture(
        company_name="Mood Pilot",
        fixture_json={
            "name": "Mood Pilot",
            "website": "https://www.moodpilot.example",
            "description": "Behavioral health app.",
            "fit": "Behavioral health relevance is plausible, but the evidence is thin.",
            "sources": [
                {
                    "source_id": "company:home",
                    "title": "Mood Pilot Home",
                    "url": "https://www.moodpilot.example",
                    "source_type": "website",
                    "supported_claims": [
                        "Mood Pilot offers a behavioral health tracking application."
                    ],
                }
            ],
        },
    )
    return company_a, company_b


def test_build_business_research_analyst_agent() -> None:
    agent = build_business_research_analyst_agent()

    assert agent.name == "business_research_analyst"
    assert "Business Research Analyst" in agent.instructions
    assert "Zotero collections" in agent.instructions
    assert "Keystone Profile" in agent.instructions
    assert getattr(agent, "output_type", CompanyProfile) is CompanyProfile
    assert {
        "load_contact_context",
        "load_crm_account_context",
        "search_web",
        "fetch_company_page",
        "extract_research_claims_from_html",
        "fetch_linkedin_or_profile_placeholder",
        "extract_company_signals",
        "dedupe_and_rank_sources",
        "build_source_bundle_for_synthesis",
        "synthesize_company_profile_from_source_bundle",
        "compare_company_profiles_for_decision",
    } <= {getattr(tool, "name", "") for tool in agent.tools}


def test_build_business_research_analyst_research_brief_agent() -> None:
    agent = build_business_research_analyst_research_brief_agent()

    assert agent.name == "business_research_analyst"
    assert getattr(agent, "output_type", ResearchBrief) is ResearchBrief
    assert "institutes, conferences, labs, people, topics, Zotero collections" in (
        agent.instructions
    )
    assert {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "search_web",
        "extract_research_claims_from_html",
        "dedupe_and_rank_sources",
    } <= {getattr(tool, "name", "") for tool in agent.tools}


def test_build_business_research_analyst_research_brief_agent_can_detach_tools() -> None:
    agent = build_business_research_analyst_research_brief_agent(attach_tools=False)

    assert agent.tools == []


def test_research_sdk_input_describes_zotero_collection_contract() -> None:
    prompt = ResearchSDKInput(
        target_name="Ketamine depression Zotero collection",
        target_type="zotero_collection",
        research_goal="Summarize mechanisms, trial designs, and evidence gaps.",
        local_context_source_ids=("zotero_import_cache",),
        source_context="Approved Zotero collection metadata snippet.",
    ).to_prompt()

    assert "Business Research Analyst source-backed brief" in prompt
    assert "Zotero article or collection requests" in prompt
    assert "zotero_import_cache" in prompt
    assert "facts[]" in prompt
    assert "Do not invent authors" in prompt


def test_retrieved_research_context_stays_out_of_agent_instructions() -> None:
    marker = "RUN-SPECIFIC-RETRIEVED-CONTEXT-MARKER"
    agent = build_business_research_analyst_research_brief_agent()
    prompt = ResearchSDKInput(
        target_name="Cache safety review",
        target_type="topic",
        research_goal="Confirm retrieved context placement.",
        source_context=f"{marker}: retrieved source bundle for this run only.",
    ).to_prompt()

    assert marker not in agent.instructions
    assert marker in prompt
    assert prompt.index("Target: Cache safety review") < prompt.index(marker)


def test_research_brief_schema_supports_article_collection_summary() -> None:
    brief = ResearchBrief(
        target_name="Ketamine depression Zotero collection",
        target_type="zotero_collection",
        research_goal="Summarize mechanisms and evidence gaps.",
        summary="The collection centers on ketamine mechanisms and clinical evidence gaps.",
        key_findings=["Mechanistic findings need source-level review before use."],
        article_summaries=[
            ResearchArticleSummary(
                title="Ketamine mechanisms review",
                source_ids=["zotero:item:ketamine-review"],
                research_question="What mechanisms are proposed for antidepressant response?",
                key_findings=["NMDA and downstream plasticity pathways are discussed."],
                limitations=["Only a metadata snippet was available."],
                relevance_to_goal="Relevant to mechanism mapping.",
            )
        ],
        facts=[
            ResearchBriefFact(
                text="The local collection includes an item titled Ketamine mechanisms review.",
                source_ids=["zotero:item:ketamine-review"],
                confidence=0.8,
            )
        ],
        limitations=["Local Zotero context is private until approved for external use."],
        sources=[
            ResearchSourceCitation(
                source_id="zotero:item:ketamine-review",
                title="Ketamine mechanisms review",
                url="local://zotero_import_cache/collection.json",
                source_type="local_zotero",
            )
        ],
    )

    assert brief.send_enabled is False
    assert brief.raw_source_content_included is False
    assert brief.source_ids_used == ["zotero:item:ketamine-review"]


def test_build_business_research_analyst_focused_brief_agent() -> None:
    agent = build_business_research_analyst_focused_brief_agent()

    assert agent.name == "business_research_analyst"
    assert "Focused Brief Mode" in agent.instructions
    assert getattr(agent, "output_type", CompanyResearchFocusedBrief) is (
        CompanyResearchFocusedBrief
    )
    assert {
        "dedupe_and_rank_sources",
        "build_source_bundle_for_synthesis",
        "synthesize_company_profile_from_source_bundle",
        "compare_company_profiles_for_decision",
    } <= {getattr(tool, "name", "") for tool in agent.tools}


def test_build_business_research_analyst_comparison_agent() -> None:
    agent = build_business_research_analyst_comparison_agent()

    assert agent.name == "business_research_analyst"
    assert getattr(agent, "output_type", CompanyResearchComparison) is (CompanyResearchComparison)
    assert "CompanyResearchComparison" in agent.instructions
    assert "compare_company_profiles_for_decision" in {
        getattr(tool, "name", "") for tool in agent.tools
    }


def test_comparison_input_preserves_structured_profiles_and_stage_checks() -> None:
    company_a = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )
    company_b = research_company_fixture(
        company_name="NeuroFlow",
        fixture_json=FIXTURES / "sample_company_neuroflow.json",
    )
    comparison = compare_company_profiles_for_decision(
        company_a,
        company_b,
        criteria=("consulting_fit", "clinical_relevance"),
    ).model_copy(
        update={
            "evidence_gaps": [
                "Both companies: website URL not supplied in the comparison input.",
                "Curebase: Behavioral health relevance is not confirmed.",
            ]
        }
    )

    prompt = comparison_input_from_result(comparison).to_prompt()

    assert "Company A: Curebase" in prompt
    assert "Company B: NeuroFlow" in prompt
    assert "deterministic_baseline" in prompt
    assert "stage_data_checks" in prompt
    assert "source_id" in prompt
    assert "website URL not supplied" not in prompt
    assert "Behavioral health relevance" in prompt


def test_focused_brief_input_contains_cr1_contract_and_source_context() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )

    prompt = focused_brief_input_from_profile(profile).to_prompt()

    assert "BR-1 focused brief" in prompt
    assert "facts[]" in prompt
    assert "inferences[]" in prompt
    assert "unknowns" in prompt
    assert "fixture:curebase_company" in prompt
    assert "fixture://sample_company_curebase.json" in prompt
    assert "Do not invent facts" in prompt


def test_focused_brief_schema_and_renderer_match_cr1_sections() -> None:
    brief = CompanyResearchFocusedBrief(
        company_name="Curebase",
        product="Clinical trial software platform.",
        customers="Clinical research teams and trial sponsors.",
        traction_signals="Partnership and validation signals are source-backed.",
        leadership="Unknown from provided sources.",
        why_it_matters="Inference: evidence-generation work may matter to Keystone.",
        facts=[
            CompanyBriefFact(
                text="Curebase is a clinical trial software company.",
                source_ids=["fixture:curebase_company"],
                confidence=0.82,
            )
        ],
        contact_candidates=[
            CompanyBriefContactCandidate(
                name="Jane Doe",
                title="Partnerships lead",
                email="jane@example.com",
                source_url="fixture://sample_company_curebase.json",
                source_ids=["fixture:curebase_company"],
                confidence=0.7,
                verification_status="source_backed",
            )
        ],
        inferences=["Potential advisory relevance is based on evidence-generation needs."],
        unknowns=["Leadership was not source-backed in the provided context."],
        sources=[
            CompanyBriefSourceCitation(
                source_id="fixture:curebase_company",
                title="Curebase fixture company profile",
                url="fixture://sample_company_curebase.json",
                source_type="fixture",
            )
        ],
    )

    markdown = render_company_focused_brief(brief)

    assert "## Product" in markdown
    assert "## Customers" in markdown
    assert "## Traction Signals" in markdown
    assert "## Leadership" in markdown
    assert "## Why It May Matter" in markdown
    assert "## Facts" in markdown
    assert "Jane Doe" in markdown
    assert "jane@example.com" in markdown
    assert "## Inferences" in markdown
    assert "## Unknowns" in markdown
    assert "fixture:curebase_company" in markdown


def test_company_profile_fixture_can_validate() -> None:
    data = json.loads((FIXTURES / "sample_company_curebase.json").read_text(encoding="utf-8"))
    profile = CompanyProfile(
        name=data["name"],
        website=data["website"],
        description=data["description"],
        fit_summary=data["fit"],
    )

    assert profile.name == "Curebase"


def test_curebase_like_fixture_scores_high_for_trial_tech_and_evidence_generation() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )

    assert profile.clinical_ai_relevance >= 70
    assert profile.evidence_generation_need >= 70
    assert profile.consulting_fit_score >= 70
    assert profile.sources


def test_complete_fixture_research_data_points_are_source_backed() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )
    data_points = {data_point.key: data_point for data_point in profile.research_data_points}
    source_ids = {source.source_id for source in profile.sources}

    assert set(data_points) == set(DEFAULT_RESEARCH_DATA_POINT_KEYS)
    assert data_points["business_model"].completed is True
    assert data_points["business_model"].confidence >= 0.8
    assert data_points["research_signals"].completed is True
    assert data_points["behavioral_health_relevance"].completed is False
    assert data_points["behavioral_health_relevance"].missing_reason
    for data_point in profile.research_data_points:
        if data_point.completed:
            assert data_point.source_ids
            assert set(data_point.source_ids) <= source_ids
            assert data_point.confidence > 0


def test_neuroflow_like_fixture_scores_high_for_behavioral_health() -> None:
    profile = research_company_fixture(
        company_name="NeuroFlow",
        fixture_json=FIXTURES / "sample_company_neuroflow.json",
    )

    assert profile.behavioral_health_relevance >= 70
    assert profile.consulting_fit_score >= 60
    assert profile.sources


def test_partial_fixture_keeps_missing_data_points_explicit() -> None:
    profile = research_company_fixture(
        company_name="NeuroFlow",
        fixture_json=FIXTURES / "sample_company_neuroflow.json",
    )
    data_points = {data_point.key: data_point for data_point in profile.research_data_points}

    assert data_points["behavioral_health_relevance"].completed is True
    assert data_points["research_signals"].completed is False
    assert data_points["funding_growth_signal"].completed is False
    assert "research" in data_points["research_signals"].missing_reason.lower()
    assert any("research data points" in item.lower() for item in profile.missing_information)


def test_irrelevant_company_scores_low() -> None:
    profile = research_company_fixture(
        company_name="Office Supplies Example Co.",
        fixture_json=FIXTURES / "sample_company_irrelevant.json",
    )

    assert profile.consulting_fit_score <= 35
    assert profile.behavioral_health_relevance <= 20
    assert profile.clinical_ai_relevance <= 20
    assert profile.sources


def test_low_confidence_profile_reflects_poor_data_point_coverage() -> None:
    profile = research_company_fixture(
        company_name="Office Supplies Example Co.",
        fixture_json=FIXTURES / "sample_company_irrelevant.json",
    )

    completed = [data_point for data_point in profile.research_data_points if data_point.completed]
    assert len(completed) <= 2
    assert profile.confidence_score < 0.75
    assert any("data point coverage" in risk.lower() for risk in profile.risks)


def test_fixture_mode_does_not_create_external_hallucinated_sources() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )

    assert all(source.url.startswith("fixture://") for source in profile.sources)
    assert all(source.supported_claims for source in profile.sources)


def test_company_profile_markdown_shows_research_data_points() -> None:
    profile = research_company_fixture(
        company_name="NeuroFlow",
        fixture_json=FIXTURES / "sample_company_neuroflow.json",
    )

    markdown = company_profile_markdown(profile)

    assert "## Research Data Points" in markdown
    assert "Business model:" in markdown
    assert "Research signals: Missing" in markdown


def test_compare_company_profiles_for_decision_is_side_by_side_and_gap_aware() -> None:
    company_a, company_b = _comparison_fixture_profiles()

    comparison = compare_company_profiles_for_decision(
        company_a,
        company_b,
        criteria=[
            "consulting_fit",
            "evidence_strength",
            "evidence_generation_need",
        ],
    )

    assert isinstance(comparison, CompanyResearchComparison)
    assert comparison.company_a.name == "Signal Trial"
    assert comparison.company_b.name == "Mood Pilot"
    assert comparison.recommended_company == "company_a"
    assert comparison.decision_criteria == [
        "consulting_fit",
        "evidence_strength",
        "evidence_generation_need",
    ]
    assert len(comparison.side_by_side_entries) == 3
    assert all(
        entry.company_a_source_ids or entry.company_b_source_ids
        for entry in comparison.side_by_side_entries
    )
    assert any(entry.better_fit == "company_a" for entry in comparison.side_by_side_entries)
    assert comparison.evidence_gaps
    assert any("Mood Pilot" in gap for gap in comparison.evidence_gaps)
    assert "Signal Trial" in comparison.recommendation


def test_company_research_strict_format_uses_requested_sections_only() -> None:
    company_a, company_b = _comparison_fixture_profiles()
    comparison = compare_company_profiles_for_decision(
        company_a,
        company_b,
        requested_output_format="summary, evidence, concerns, next step",
    )

    markdown = company_research_comparison_markdown(
        comparison,
        strict_format=True,
    )

    non_empty_lines = [line for line in markdown.splitlines() if line.strip()]
    assert non_empty_lines[0] == "summary"
    assert "evidence" in non_empty_lines
    assert "concerns" in non_empty_lines
    assert "next step" in non_empty_lines
    assert "## Summary" not in markdown
    assert "## Criteria" not in markdown
    assert "company:about" in markdown


def test_company_claims_reference_source_ids() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )
    source_ids = {source.source_id for source in profile.sources}

    assert profile.claims
    assert all(claim.claim_text for claim in profile.claims)
    assert all(claim.source_id in source_ids for claim in profile.claims)
    assert all(0.0 <= claim.confidence <= 1.0 for claim in profile.claims)
    assert {claim.claim_type for claim in profile.claims}


def test_company_features_are_captured_and_only_source_backed_features_score() -> None:
    clinical_feature_claim = (
        "FeatureBacked builds a clinical AI platform for clinical trial evidence generation."
    )
    neutral_claim = "FeatureBacked is an administrative services company."
    backed_profile = research_company_fixture(
        company_name="FeatureBacked",
        fixture_json={
            "name": "FeatureBacked",
            "sources": [
                {
                    "source_id": "fixture:featurebacked",
                    "title": "FeatureBacked fixture",
                    "url": "fixture://featurebacked",
                    "source_type": "fixture",
                    "supported_claims": [clinical_feature_claim],
                }
            ],
            "claims": [
                {
                    "claim_text": neutral_claim,
                    "source_id": "fixture:featurebacked",
                    "claim_type": "company_description",
                    "confidence": 0.7,
                }
            ],
            "company_features": [
                {
                    "feature_name": "ai_maturity",
                    "value": "clinical AI platform",
                    "source_id": "fixture:featurebacked",
                    "evidence_text": clinical_feature_claim,
                    "confidence": 0.8,
                }
            ],
        },
    )
    unbacked_profile = research_company_fixture(
        company_name="FeatureBacked",
        fixture_json={
            "name": "FeatureBacked",
            "sources": [
                {
                    "source_id": "fixture:featurebacked",
                    "title": "FeatureBacked fixture",
                    "url": "fixture://featurebacked",
                    "source_type": "fixture",
                    "supported_claims": [neutral_claim],
                }
            ],
            "claims": [
                {
                    "claim_text": neutral_claim,
                    "source_id": "fixture:featurebacked",
                    "claim_type": "company_description",
                    "confidence": 0.7,
                }
            ],
            "company_features": [
                {
                    "feature_name": "ai_maturity",
                    "value": "clinical AI platform",
                    "source_id": "fixture:missing",
                    "evidence_text": clinical_feature_claim,
                    "confidence": 0.8,
                }
            ],
        },
    )

    assert {feature.feature_name for feature in backed_profile.features} >= {
        "ai_maturity",
        "clinical_research_relevance",
    }
    assert any(
        feature.feature_name == "ai_maturity" for feature in backed_profile.source_backed_features
    )
    assert not any(
        feature.feature_name == "ai_maturity" for feature in unbacked_profile.source_backed_features
    )
    assert any(
        "unbacked company feature: ai_maturity" in flag
        for flag in unbacked_profile.unsupported_claims_flagged
    )
    assert backed_profile.consulting_fit_score > unbacked_profile.consulting_fit_score
    assert backed_profile.confidence_score > unbacked_profile.confidence_score


def test_company_research_queries_cover_mira_style_sources() -> None:
    queries = build_company_research_queries("Curebase", "https://www.curebase.com")
    query_text = "\n".join(queries).lower()

    assert "official website" in query_text or "site:https://www.curebase.com" in query_text
    assert "linkedin" in query_text
    assert "site:linkedin.com/company" in query_text
    assert "recent news funding" in query_text
    assert "funding valuation headcount" in query_text
    assert "business model customers" in query_text
    assert "partners customers health systems employers payer" in query_text
    assert "contact email business development partnerships" in query_text
    assert "prnewswire.com" in query_text
    assert "clinical validation" in query_text
    assert "product launch" in query_text
    assert "peer reviewed validation study outcomes" in query_text
    assert "differentiator unique approach" in query_text


def test_company_research_queries_prioritize_requested_official_source_lanes() -> None:
    queries = build_company_research_queries(
        "Curebase",
        "https://www.curebase.com",
        request_text=(
            "Review Curebase clinical trials, peer-reviewed studies, leadership, and hiring."
        ),
    )

    assert queries[4:11] == [
        "Curebase official careers jobs",
        "site:boards.greenhouse.io Curebase",
        "site:jobs.lever.co Curebase",
        "site:jobs.ashbyhq.com Curebase",
        "site:clinicaltrials.gov Curebase study sponsor recruiting",
        "site:pubmed.ncbi.nlm.nih.gov Curebase study outcomes validation",
        "Curebase official leadership founders executives team",
    ]


def test_business_research_analyst_consumes_mocked_search_results_and_preserves_sources() -> None:
    results = [
        SearchResult(
            title="Curebase launches clinical trial workflow update",
            link="https://example.com/curebase-launch",
            snippet=(
                "Curebase announced clinical trial workflow tooling for decentralized research."
            ),
            source="serper",
        ),
        SearchResult(
            title="Curebase LinkedIn profile",
            link="https://www.linkedin.com/company/curebase",
            snippet="Company profile describes clinical trial software and research operations.",
            source="serper",
        ),
    ]

    profile = research_account_from_search_results(
        company_name="Curebase",
        company_url="https://www.curebase.com",
        search_results=results,
    )

    assert isinstance(profile, CompanyProfile)
    assert any(source.url == "https://example.com/curebase-launch" for source in profile.sources)
    assert any(
        source.url == "https://www.linkedin.com/company/curebase" for source in profile.sources
    )
    assert any("clinical trial workflow" in claim for claim in profile.evidence)


def test_dedupe_and_rank_sources_prefers_source_backed_company_and_news() -> None:
    ranked = dedupe_and_rank_sources(
        company_name="RankTrial",
        company_url="https://www.ranktrial.example",
        source_payloads=[
            {
                "source_id": "social:1",
                "title": "Founder post",
                "url": "https://x.com/ranktrial/status/1",
                "source_type": "social",
                "claims": ["RankTrial may be building a clinical AI product."],
            },
            {
                "source_id": "company:about",
                "title": "RankTrial About",
                "url": "https://www.ranktrial.example/about",
                "source_type": "website",
                "claims": [
                    "RankTrial builds clinical trial workflow software for evidence generation."
                ],
            },
            {
                "source_id": "company:about-copy",
                "title": "RankTrial About Duplicate",
                "url": "https://www.ranktrial.example/about",
                "source_type": "website",
                "claims": ["RankTrial supports decentralized trial operations."],
            },
            {
                "source_id": "news:launch",
                "title": "RankTrial validation launch",
                "url": "https://www.reuters.com/example/ranktrial-validation",
                "source_type": "news",
                "published_at": "2026-03-01",
                "claims": ["RankTrial announced a clinical validation partnership."],
            },
        ],
    )

    assert [source["source_id"] for source in ranked[:2]] == ["company:about", "news:launch"]
    assert len(ranked) == 3
    assert ranked[0]["source_type"] == "company_site"
    assert "decentralized trial operations" in " ".join(ranked[0]["supported_claims"])


def test_page_text_enrichment_extracts_clean_source_claims() -> None:
    profile = research_account_from_search_results(
        company_name="CleanTrial",
        company_url="https://www.cleantrial.example",
        website_inputs=[
            {
                "source_id": "company:page",
                "url": "https://www.cleantrial.example/about",
                "title": "CleanTrial About",
                "source_type": "website",
                "html": (
                    "<html><head><style>.hidden{}</style><script>secret()</script></head>"
                    "<body><h1>CleanTrial</h1>"
                    "<p>CleanTrial builds clinical trial workflow software for evidence "
                    "generation.</p>"
                    "<p>CleanTrial supports research operations and validation studies.</p>"
                    "</body></html>"
                ),
            }
        ],
    )

    evidence = "\n".join(profile.evidence)
    page_source = next(source for source in profile.sources if source.source_id == "company:page")
    assert "secret()" not in evidence
    assert "clinical trial workflow software" in evidence
    assert "secret()" not in page_source.evidence_excerpt
    assert "supports research operations" in page_source.evidence_excerpt


def test_website_input_does_not_gain_a_synthetic_fixture_source() -> None:
    profile = research_account_from_search_results(
        company_name="DirectSource Trial",
        company_url="https://www.directsource.example",
        website_inputs=[
            {
                "source_id": "direct:official-company-page",
                "url": "https://www.directsource.example",
                "title": "DirectSource Trial",
                "source_type": "company_site",
                "claims": [
                    "DirectSource Trial provides behavioral health analytics to providers."
                ],
            }
        ],
    )

    assert [source.source_id for source in profile.sources] == [
        "direct:official-company-page"
    ]
    assert {claim.source_id for claim in profile.claims} == {
        "direct:official-company-page"
    }


def test_business_research_analyst_adds_approved_local_contact_and_crm_context() -> None:
    profile = research_account_from_search_results(
        company_name="Curebase",
        company_url="https://www.curebase.com",
        contact_context=load_contact_context("sample_contact_curebase_approved"),
        crm_context=load_crm_account_context("sample_crm_context_curebase"),
    )

    source_ids = {source.source_id for source in profile.sources}
    assert profile.lead_name == "Dr. Priya Shah"
    assert "fixture:contact_curebase_priya" in source_ids
    assert "fixture:crm_context_curebase" in source_ids
    assert any("Dr. Priya Shah" in claim.claim_text for claim in profile.claims)
    assert any("low-pressure research workflow" in claim for claim in profile.evidence)


def test_business_research_analyst_flags_unapproved_contact_context_without_using_it() -> None:
    profile = research_account_from_search_results(
        company_name="Curebase",
        company_url="https://www.curebase.com",
        contact_context=load_contact_context("sample_contact_curebase_unapproved"),
    )

    assert profile.lead_name is None
    assert not any(
        source.source_id == "fixture:contact_curebase_unapproved" for source in profile.sources
    )
    assert any("unapproved contact context" in flag for flag in profile.unsupported_claims_flagged)


def test_business_research_analyst_records_source_contradictions() -> None:
    profile = research_account_from_search_results(
        company_name="ContradictTrial",
        company_url="https://www.contradicttrial.example",
        website_inputs=[
            {
                "source_id": "company:about",
                "url": "https://www.contradicttrial.example/about",
                "title": "ContradictTrial About",
                "source_type": "website",
                "claims": ["ContradictTrial builds clinical trial software."],
            },
            {
                "source_id": "news:correction",
                "url": "https://www.reuters.com/example/contradicttrial-correction",
                "title": "ContradictTrial correction",
                "source_type": "news",
                "claims": ["ContradictTrial says it is not a clinical trial software company."],
            },
        ],
    )

    assert profile.contradictions
    assert "clinical trial" in profile.contradictions[0]
    assert any("Contradictory" in risk for risk in profile.risks)
    assert "contradictions=" in profile.confidence_explanation


def test_business_research_analyst_surfaces_funding_and_operational_status_conflicts() -> None:
    profile = research_account_from_search_results(
        company_name="Pear Therapeutics",
        company_url="https://www.peartherapeutics.com",
        website_inputs=[
            {
                "source_id": "company:overview",
                "url": "https://www.peartherapeutics.com/company",
                "title": "Pear company overview",
                "source_type": "website",
                "claims": [
                    "Pear Therapeutics offers prescription digital therapeutics products.",
                    "Pear Therapeutics has raised $266M in funding.",
                ],
            },
            {
                "source_id": "news:bankruptcy",
                "url": "https://www.reuters.com/example/pear-bankruptcy",
                "title": "Pear bankruptcy filing",
                "source_type": "news",
                "claims": [
                    "Pear Therapeutics raised $396M before filing Chapter 11 bankruptcy.",
                    "Pear Therapeutics is winding down operations after bankruptcy.",
                ],
            },
        ],
    )

    contradiction_text = "\n".join(profile.contradictions)

    assert "Funding totals conflict" in contradiction_text
    assert "Operational or product status" in contradiction_text
    assert any("Contradictory" in risk for risk in profile.risks)
    assert "contradictions=" in profile.confidence_explanation
    assert profile.confidence_score < 0.9


def test_stale_source_adds_missing_evidence_review_signal() -> None:
    profile = research_account_from_search_results(
        company_name="StaleTrial",
        company_url="https://www.staletrial.example",
        website_inputs=[
            {
                "source_id": "company:archive",
                "url": "https://www.staletrial.example/about",
                "title": "StaleTrial Archive",
                "source_type": "website",
                "published_at": "2018-01-01",
                "claims": ["StaleTrial builds clinical trial software for evidence generation."],
            }
        ],
    )

    assert profile.sources[0].source_quality is not None
    assert profile.sources[0].source_quality.recency_score < 50
    assert any("Stale source evidence" in item for item in profile.missing_evidence)


def test_source_bundle_synthesis_uses_only_source_records() -> None:
    bundle = build_source_bundle_for_synthesis(
        company_name="BundleTrial",
        company_url="https://www.bundletrial.example",
        source_payloads=[
            {
                "source_id": "company:about",
                "url": "https://www.bundletrial.example/about",
                "title": "BundleTrial About",
                "source_type": "website",
                "claims": ["BundleTrial builds clinical AI software for evidence generation."],
            }
        ],
    )

    profile = synthesize_company_profile_from_source_bundle(
        company_name="BundleTrial",
        company_url="https://www.bundletrial.example",
        source_bundle={
            **bundle,
            "unsupported_note": "Keystone has helped BundleTrial deliver proven results.",
        },
    )

    assert profile.evidence == ["BundleTrial builds clinical AI software for evidence generation."]
    assert profile.description == "BundleTrial builds clinical AI software for evidence generation."
    assert "proven results" not in "\n".join(profile.evidence).lower()
    assert all(claim.source_id == "company:about" for claim in profile.claims)


def test_source_bundle_serialization_is_deterministic_for_identical_inputs() -> None:
    source_payloads = [
        {
            "source_id": "company:about",
            "url": "https://www.bundletrial.example/about",
            "title": "BundleTrial About",
            "source_type": "website",
            "claims": ["BundleTrial builds clinical AI software for evidence generation."],
        },
        {
            "source_id": "news:launch",
            "url": "https://www.reuters.com/example/bundletrial-launch",
            "title": "BundleTrial launch",
            "source_type": "news",
            "published_at": "2026-04-01",
            "claims": ["BundleTrial announced a clinical validation partnership."],
        },
    ]

    first = build_source_bundle_for_synthesis(
        company_name="BundleTrial",
        company_url="https://www.bundletrial.example",
        source_payloads=source_payloads,
    )
    second = build_source_bundle_for_synthesis(
        company_name="BundleTrial",
        company_url="https://www.bundletrial.example",
        source_payloads=source_payloads,
    )

    assert json.dumps(first, ensure_ascii=True, sort_keys=True) == json.dumps(
        second,
        ensure_ascii=True,
        sort_keys=True,
    )


def test_multi_source_research_aggregation_scores_high_confidence() -> None:
    profile = research_account_from_search_results(
        company_name="DeepTrial",
        company_url="https://www.deeptrial.example",
        website_inputs=[
            {
                "url": "https://www.deeptrial.example/about",
                "title": "DeepTrial clinical AI platform",
                "source_type": "website",
                "claims": [
                    "DeepTrial builds clinical AI trial workflow software for evidence generation."
                ],
            }
        ],
        profile_inputs=[
            {
                "linkedin_url": "https://www.linkedin.com/company/deeptrial",
                "title": "DeepTrial LinkedIn profile",
                "claims": [
                    "DeepTrial describes its work as clinical research operations software."
                ],
            }
        ],
        search_results=[
            SearchResult(
                title="DeepTrial validation partnership",
                link="https://www.reuters.com/example/deeptrial-validation",
                snippet=(
                    "DeepTrial announced a clinical validation partnership for trial workflow "
                    "automation."
                ),
                source="serper",
            )
        ],
    )

    assert profile.confidence_score >= 0.75
    assert profile.source_quality_summary is not None
    assert profile.source_quality_summary.independent_source_count >= 3
    assert profile.consulting_fit_score >= 70
    assert len([point for point in profile.research_data_points if point.completed]) >= 6
    assert len(profile.sources) >= 3
    assert not any(source.source_id.startswith("fixture:") for source in profile.sources)
    source_ids = {source.source_id for source in profile.sources}
    assert all(claim.source_id in source_ids for claim in profile.claims)


def test_high_quality_research_stops_after_trusted_website_and_news() -> None:
    profile = research_company_fixture(
        company_name="TrustTrial",
        fixture_json={
            "name": "TrustTrial",
            "website": "https://www.trusttrial.example",
            "linkedin_url": "https://www.linkedin.com/company/trusttrial",
            "sources": [
                {
                    "source_id": "company:about",
                    "title": "TrustTrial About",
                    "url": "https://www.trusttrial.example/about",
                    "source_type": "website",
                    "published_at": "2026-03-01",
                    "supported_claims": [
                        "TrustTrial builds clinical trial software for evidence generation.",
                        "TrustTrial supports decentralized clinical trial operations.",
                    ],
                },
                {
                    "source_id": "news:launch",
                    "title": "TrustTrial launches validation partnership",
                    "url": (
                        "https://www.reuters.com/business/healthcare-pharmaceuticals/"
                        "trusttrial-validation-2026-03-15/"
                    ),
                    "source_type": "news",
                    "published_at": "2026-03-15",
                    "supported_claims": [
                        (
                            "TrustTrial announced a clinical validation partnership for "
                            "trial workflow automation."
                        )
                    ],
                },
                {
                    "source_id": "academic:study",
                    "title": "TrustTrial validation study",
                    "url": "https://clinicaltrials.gov/study/NCT00000001",
                    "source_type": "academic",
                    "published_at": "2026-02-01",
                    "supported_claims": [
                        "A study record references TrustTrial trial workflow validation."
                    ],
                },
                {
                    "source_id": "social:rumor",
                    "title": "Founder social post",
                    "url": "https://x.com/trusttrial/status/1",
                    "source_type": "social",
                    "published_at": "2026-04-01",
                    "supported_claims": [
                        (
                            "A social post claims TrustTrial has an unverified hospital "
                            "customer outcome."
                        )
                    ],
                },
            ],
        },
    )

    source_ids = {source.source_id for source in profile.sources}
    assert profile.research_completeness is not None
    assert profile.research_completeness.stop_recommended is True
    assert profile.research_completeness.score >= 82
    assert "company:about" in source_ids
    assert "news:launch" in source_ids
    assert "academic:study" not in source_ids
    assert "social:rumor" not in source_ids
    assert "fixture/mock research stop condition met" in profile.confidence_explanation


def test_low_confidence_company_research_makes_gaps_explicit() -> None:
    profile = research_company_fixture(
        company_name="Unverified Health AI Co.",
        fixture_json={
            "name": "Unverified Health AI Co.",
            "description": "Unverified AI services company.",
            "sources": [
                {
                    "source_id": "social:1",
                    "title": "Founder social post",
                    "url": "https://x.com/example/status/1",
                    "source_type": "social",
                    "supported_claims": ["A social post says the company may work on health AI."],
                }
            ],
        },
    )

    assert profile.confidence_score <= 0.55
    assert profile.research_completeness is not None
    assert profile.research_completeness.score < 50
    assert "trusted company website evidence" in profile.research_completeness.missing_dimensions
    assert "source_quality=" in profile.confidence_explanation
    assert any("corroboration" in item.lower() for item in profile.missing_information)
    assert any("low confidence" in item.lower() for item in profile.risks)


def test_single_source_company_website_remains_limited_confidence() -> None:
    profile = research_company_fixture(
        company_name="SingleSource Trial",
        fixture_json={
            "name": "SingleSource Trial",
            "website": "https://www.singlesource.example",
            "sources": [
                {
                    "source_id": "company:about",
                    "title": "SingleSource Trial About",
                    "url": "https://www.singlesource.example/about",
                    "source_type": "website",
                    "published_at": "2026-03-01",
                    "supported_claims": [
                        (
                            "SingleSource builds clinical trial workflow software for "
                            "evidence generation."
                        ),
                        "SingleSource supports decentralized trial operations.",
                    ],
                }
            ],
        },
    )

    assert profile.source_quality_summary is not None
    assert profile.source_quality_summary.high_quality_source_count == 1
    assert profile.research_completeness is not None
    assert profile.research_completeness.stop_recommended is False
    assert profile.confidence_score <= 0.7
    assert "limited independent corroboration" in profile.confidence_explanation


def test_unsupported_keystone_outcome_claim_is_flagged_not_used() -> None:
    profile = research_company_fixture(
        company_name="TrialOps Example",
        fixture_json={
            "name": "TrialOps Example",
            "website": "https://www.trialops.example",
            "sources": [
                {
                    "source_id": "fixture:trialops",
                    "title": "TrialOps fixture",
                    "url": "fixture://trialops",
                    "source_type": "fixture",
                    "supported_claims": [
                        "TrialOps Example builds clinical trial workflow software.",
                        "Keystone has helped clinical trial companies reduce enrollment delays.",
                    ],
                }
            ],
        },
    )

    evidence_text = "\n".join(profile.evidence).lower()
    assert profile.unsupported_claims_flagged
    assert "reduce enrollment delays" not in evidence_text
    assert any("clinical trial workflow software" in claim.lower() for claim in profile.evidence)


def test_business_research_analyst_sdk_search_tool_is_not_live_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "test-key-that-must-not-be-used")

    def fail_post(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Business Research Analyst SDK search tool must not call the network")

    monkeypatch.setattr("keystone_agents.tools.serper_tool.requests.post", fail_post)

    agent = build_business_research_analyst_agent()
    assert any(getattr(tool, "name", "") == "search_web" for tool in agent.tools)

    assert search_web("Curebase clinical trial software", num_results=1) == []
